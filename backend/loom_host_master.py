#!/usr/bin/env python3
"""
Autonomous SMIT loom host/master application with interactive dashboard.

Built on top of loom_host_v2.py and adds:
- dashboard command buttons for monitoring, configuration, and selected write actions
- compact modern UI with capabilities and live-events pages
- subcommands for service mode, self-test, and one-shot TS reads/writes

Typical use:
    python loom_host_master.py --config loom_host_master_config.example.json run --log-level INFO
    python loom_host_master.py --config loom_host_master_config.example.json selftest
    python loom_host_master.py --config loom_host_master_config.example.json status --loom-ip 169.254.4.101
    python loom_host_master.py --config loom_host_master_config.example.json speed --loom-ip 169.254.4.101
    python loom_host_master.py --config loom_host_master_config.example.json popup --loom-ip 169.254.4.101 --message "Host online"
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import ftplib
import hashlib
import html
import io
import json
import logging
import re
import signal
import shutil
import socket
import platform
import sys
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import loom_host_v2 as core

# Additional commands implemented here.
CMD_PATTERN = 0x1A
CMD_BASIC_CONFIG = 0x1B
CMD_TOTAL_PICKS = 0x21
CMD_LAMP = 0x28
CMD_PRESELECTION = 0x33
CMD_REMOTE_CONTROL = 0x42
CMD_DENSITY = 0x96

DEFAULT_FTP_PORT = 21
DEFAULT_FTP_USER = "root"
DEFAULT_FTP_PASSWORD = "root"
DEFAULT_FTP_APP_DIR = "/usr/LOOM"

BEIJING_TZ = timezone(timedelta(hours=8))
RELEASE_VERSION = "release_v2_ui"
RELEASE_DATE = "2026-04-21"
MIN_MONITOR_ROLE = "monitor"
MIN_WRITE_ROLE = "write"

def bj_now_iso() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")

def to_beijing_time_text(value: Any) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(value)
    s = str(value).strip()
    if not s:
        return "—"
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc).astimezone(BEIJING_TZ)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        if "T" in s:
            return (dt.replace(tzinfo=timezone.utc).astimezone(BEIJING_TZ)).strftime("%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s

RESULT_TEXT = {
    0x00: "OK",
    0x01: "OK",
    0x02: "OK",
    0x03: "OK",
    0x04: "OK",
    0x55: "OK",
    0x77: "Invalid pattern / Testana name",
    0x88: "Parameter out of range",
    0x99: "Bad data format",
    0xAA: "Communication error with loom",
    0xBB: "Motor OFF",
    0xCC: "Loom weaving / busy",
}

UNIT_PRESELECTION = {
    1: "picks",
    2: "decimetres",
    3: "yards",
    4: "pattern repeats",
    5: "metres",
}

UNIT_DENSITY = {
    0: "weft/cm",
    1: "weft/dm",
    2: "weft/inch",
}

PATTERN_FORMAT = {
    0xA5: "Flat / standard",
    0xA6: "Terry / extended",
    0x6A: "Terry / reduced",
}


WRITE_AUTH_USER = "smit"
WRITE_AUTH_PASSWORD = "2fast"
WRITE_AUTH_COOKIE = "loom_write_auth"
WRITE_AUTH_SECRET = "smit-2fast-dashboard-auth-v1"
WRITE_ACTION_PATHS = {
    "/action/pattern-send",
    "/action/lamp-write",
    "/action/preselection-write",
    "/action/preselection-reset",
    "/action/admin-declaration",
    "/action/remote-stop",
}


class InteractiveDashboardServer(core.HttpDashboardServer):
    def __init__(
        self,
        app: "MasterApp",
        registry: core.LoomRegistry,
        sink: core.EventSink,
        store: Optional[core.SQLiteStore],
        logger: logging.Logger,
    ) -> None:
        super().__init__(registry, sink, store, logger)
        self.app = app

    @staticmethod
    def _today_key() -> str:
        return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    @classmethod
    def _cookie_max_age(cls) -> int:
        now = datetime.now(BEIJING_TZ)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(60, int((tomorrow - now).total_seconds()))

    @classmethod
    def _make_auth_cookie_value(cls) -> str:
        day = cls._today_key()
        sig = hashlib.sha256(f"{WRITE_AUTH_USER}|{day}|{WRITE_AUTH_SECRET}".encode("utf-8")).hexdigest()
        return f"{day}.{sig}"

    @classmethod
    def _auth_cookie_valid(cls, cookie_header: str) -> bool:
        if not cookie_header:
            return False
        jar = SimpleCookie()
        try:
            jar.load(cookie_header)
        except Exception:
            return False
        morsel = jar.get(WRITE_AUTH_COOKIE)
        if not morsel:
            return False
        value = morsel.value
        if "." not in value:
            return False
        day, sig = value.split(".", 1)
        expected = hashlib.sha256(f"{WRITE_AUTH_USER}|{day}|{WRITE_AUTH_SECRET}".encode("utf-8")).hexdigest()
        return day == cls._today_key() and sig == expected

    @staticmethod
    def _is_write_action(path: str) -> bool:
        return path in WRITE_ACTION_PATHS

    async def _send_response_ex(self, writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes, extra_headers: Optional[List[Tuple[str, str]]] = None) -> None:
        reason = {
            200: "OK",
            401: "Unauthorized",
            403: "Forbidden",
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
        ]
        for key, value in (extra_headers or []):
            headers.append(f"{key}: {value}")
        headers.extend(["", ""])
        writer.write("\r\n".join(headers).encode("ascii") + body)
        await writer.drain()

    async def _json_ex(self, writer: asyncio.StreamWriter, data: Dict[str, Any], status: int = 200, extra_headers: Optional[List[Tuple[str, str]]] = None) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        await self._send_response_ex(writer, status, "application/json; charset=utf-8", body, extra_headers=extra_headers)

    @staticmethod
    def _logo_file_candidates() -> List[Path]:
        base = Path(__file__).resolve().parent
        names = ["LOGO_SMIT_R.JPG", "LOGO_SMIT_R.jpg", "logo_smit_r.jpg", "logo_smit_r.jpeg", "logo.jpg", "logo.jpeg", "logo.png"]
        return [base / n for n in names]

    @classmethod
    def _load_logo_asset(cls) -> Optional[Tuple[bytes, str]]:
        for path in cls._logo_file_candidates():
            if path.exists():
                suffix = path.suffix.lower()
                content_type = "image/png" if suffix == ".png" else "image/jpeg"
                return path.read_bytes(), content_type
        return None

    @staticmethod
    def _display_loom_name(name: Any) -> str:
        raw = "" if name is None else str(name)
        return "2fast-loom-01" if raw == "workshop-loom-01" else raw

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            header_text = head.decode("iso-8859-1", errors="replace")
            first_line = header_text.split("\r\n", 1)[0]
            method, target, _ = first_line.split(" ", 2)
            headers: Dict[str, str] = {}
            for raw_line in header_text.split("\r\n")[1:]:
                if not raw_line:
                    continue
                if ":" in raw_line:
                    k, v = raw_line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            body = b""
            if method == "POST":
                content_length = int(headers.get("content-length", "0") or "0")
                if content_length:
                    body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)

            parsed = urlparse(target)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if method == "GET":
                if path == "/health":
                    await self._send_response(writer, 200, "text/plain; charset=utf-8", b"OK\n")
                    return
                if path == "/api/looms":
                    payload = json.dumps(self.app.dashboard_snapshot(), indent=2, default=str).encode("utf-8")
                    await self._send_response(writer, 200, "application/json; charset=utf-8", payload)
                    return
                if path == "/api/events":
                    limit = min(max(int(qs.get("limit", ["100"])[0]), 1), 1000)
                    payload = json.dumps(self.store.recent_events(limit) if self.store else [], indent=2, default=str).encode("utf-8")
                    await self._send_response(writer, 200, "application/json; charset=utf-8", payload)
                    return
                if path == "/api/frames":
                    limit = min(max(int(qs.get("limit", ["100"])[0]), 1), 1000)
                    payload = json.dumps(self.store.recent_frames(limit) if self.store else [], indent=2, default=str).encode("utf-8")
                    await self._send_response(writer, 200, "application/json; charset=utf-8", payload)
                    return
                if path == "/fragment/dashboard-cards":
                    payload = json.dumps({
                        "ok": True,
                        "generated_at": bj_now_iso(),
                        "html": self._render_dashboard_cards(self.app.dashboard_snapshot()),
                    }, ensure_ascii=False).encode("utf-8")
                    await self._send_response(writer, 200, "application/json; charset=utf-8", payload)
                    return
                if path == "/":
                    await self.app.refresh_dashboard(force=False)
                    page = self._render_dashboard_html()
                    await self._send_response(writer, 200, "text/html; charset=utf-8", page.encode("utf-8"))
                    return
                if path == "/capabilities":
                    page = self._render_capabilities_html()
                    await self._send_response(writer, 200, "text/html; charset=utf-8", page.encode("utf-8"))
                    return
                if path == "/live":
                    page = self._render_live_html()
                    await self._send_response(writer, 200, "text/html; charset=utf-8", page.encode("utf-8"))
                    return
                if path == "/machines":
                    page = self._render_machines_html()
                    await self._send_response(writer, 200, "text/html; charset=utf-8", page.encode("utf-8"))
                    return
                if path == "/diagnostics":
                    page = self._render_diagnostics_html()
                    await self._send_response(writer, 200, "text/html; charset=utf-8", page.encode("utf-8"))
                    return
                if path == "/api/diagnostics":
                    payload = json.dumps(self.app.diagnostics_snapshot(), indent=2, ensure_ascii=False, default=str).encode("utf-8")
                    await self._send_response(writer, 200, "application/json; charset=utf-8", payload)
                    return
                if path == "/static/logo.jpg":
                    asset = self._load_logo_asset()
                    if asset is None:
                        await self._send_response_ex(writer, 404, "text/plain; charset=utf-8", b"Logo not found")
                    else:
                        data, content_type = asset
                        await self._send_response_ex(writer, 200, content_type, data)
                    return
                page = self._render_dashboard_html()
                await self._send_response(writer, 200, "text/html; charset=utf-8", page.encode("utf-8"))
                return

            if method == "POST":
                await self._handle_post(path, body, writer, headers)
                return

            await self._send_response(writer, 405, "text/plain; charset=utf-8", b"Method Not Allowed")
        except asyncio.IncompleteReadError:
            pass
        except (asyncio.TimeoutError, asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            # Browser preconnects, aborted refreshes, extensions, or partial requests
            # should not be treated as dashboard server errors.
            pass
        except Exception as exc:
            self.log.exception("HTTP dashboard request failed")
            try:
                payload = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False).encode("utf-8")
                await self._send_response(writer, 500, "application/json; charset=utf-8", payload)
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_post(self, path: str, body: bytes, writer: asyncio.StreamWriter, headers: Dict[str, str]) -> None:
        form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        if path == "/auth/login":
            username = (form.get("username", [""])[0] or "").strip()
            password = (form.get("password", [""])[0] or "")
            if username == WRITE_AUTH_USER and password == WRITE_AUTH_PASSWORD:
                cookie_value = self._make_auth_cookie_value()
                max_age = self._cookie_max_age()
                cookie = f"{WRITE_AUTH_COOKIE}={cookie_value}; Max-Age={max_age}; Path=/; SameSite=Lax"
                await self._json_ex(writer, {"ok": True, "message": "Write access enabled for today"}, extra_headers=[("Set-Cookie", cookie)])
            else:
                await self._json_ex(writer, {"ok": False, "error": "Invalid username or password"}, status=403)
            return

        if self._is_write_action(path) and (not bool(self.app.config.get("write_actions_enabled", True))):
            await self._json_ex(writer, {"ok": False, "error": "Write actions are disabled by configuration"}, status=403)
            return
        if self._is_write_action(path) and (not self._auth_cookie_valid(headers.get("cookie", ""))):
            await self._json_ex(writer, {"ok": False, "error": "Write authentication required"}, status=403)
            return

        loom_ip = (form.get("loom_ip", [""])[0] or "").strip()
        message = (form.get("message", [""])[0] or "").strip()
        pattern_name = (form.get("pattern_name", [""])[0] or "").strip()
        raw_calls = (form.get("calls_mask", [""])[0] or "").strip()
        code_str = (form.get("code_str", [""])[0] or "").strip()
        template = (form.get("template", [""])[0] or "").strip()

        try:
            if path == "/action/read-all":
                await self._json(writer, {"ok": True, "result": await self.app.query_all_status(self._require_loom(loom_ip), force=True)})
                return
            if path == "/action/realtime-status":
                await self._json(writer, {"ok": True, "result": await self.app.query_realtime_status(self._require_loom(loom_ip))})
                return
            if path == "/action/error-logs":
                await self._json(writer, {"ok": True, "result": await self.app.query_error_logs(self._require_loom(loom_ip))})
                return
            if path == "/action/status":
                await self._json(writer, {"ok": True, "result": await self.app.query_status(self._require_loom(loom_ip))})
                return
            if path == "/action/full-status":
                await self._json(writer, {"ok": True, "result": await self.app.query_full_status(self._require_loom(loom_ip))})
                return
            if path == "/action/speed":
                await self._json(writer, {"ok": True, "result": await self.app.query_speed(self._require_loom(loom_ip))})
                return
            if path == "/action/basic-config":
                await self._json(writer, {"ok": True, "result": await self.app.query_basic_config(self._require_loom(loom_ip))})
                return
            if path == "/action/total-picks":
                await self._json(writer, {"ok": True, "result": await self.app.query_total_picks(self._require_loom(loom_ip))})
                return
            if path == "/action/density":
                await self._json(writer, {"ok": True, "result": await self.app.query_density(self._require_loom(loom_ip))})
                return
            if path == "/action/pattern-current":
                await self._json(writer, {"ok": True, "result": await self.app.query_pattern_current(self._require_loom(loom_ip))})
                return
            if path == "/action/pattern-info":
                await self._json(writer, {"ok": True, "result": await self.app.query_pattern_info(self._require_loom(loom_ip), self._require_text(pattern_name, "pattern_name"))})
                return
            if path == "/action/pattern-send":
                await self._json(writer, {"ok": True, "result": await self.app.send_pattern(self._require_loom(loom_ip), self._require_text(pattern_name, "pattern_name"))})
                return
            if path == "/action/lamp-read":
                await self._json(writer, {"ok": True, "result": await self.app.query_lamp_tree(self._require_loom(loom_ip))})
                return
            if path == "/action/lamp-write":
                await self._json(writer, {"ok": True, "result": await self.app.write_lamp_tree(self._require_loom(loom_ip), int(raw_calls, 0))})
                return
            if path == "/action/preselection-read":
                await self._json(writer, {"ok": True, "result": await self.app.query_preselection(self._require_loom(loom_ip))})
                return
            if path == "/action/preselection-write":
                payload = {
                    "enabled": self._as_bool(form, "enabled"),
                    "stop_on_reach": self._as_bool(form, "stop_on_reach"),
                    "confirm_before_restart": self._as_bool(form, "confirm_before_restart"),
                    "delayed_stop": self._as_bool(form, "delayed_stop"),
                    "manual_reset": self._as_bool(form, "manual_reset"),
                    "unit": int((form.get("unit", [""])[0] or "0").strip()),
                    "preselection_value": int((form.get("preselection_value", [""])[0] or "0").strip()),
                    "counter_value": int((form.get("counter_value", ["0"])[0] or "0").strip()),
                }
                await self._json(writer, {"ok": True, "result": await self.app.write_preselection(self._require_loom(loom_ip), payload)})
                return
            if path == "/action/preselection-reset":
                await self._json(writer, {"ok": True, "result": await self.app.reset_preselection(self._require_loom(loom_ip))})
                return
            if path == "/action/admin-declaration":
                result = self.app.configure_admin_declaration(
                    self._require_loom(loom_ip),
                    code_str=(code_str or "565"),
                    template=(template or "Employee ID: _____ Production Plan ID: _____ Yarn Package Number: _____"),
                )
                await self._json(writer, {"ok": True, "result": result})
                return
            if path == "/action/machine-add":
                result = self.app.upsert_loom_config(
                    old_ip=None,
                    name=(form.get("name", [""])[0] or "").strip(),
                    new_ip=self._require_text((form.get("new_ip", [""])[0] or "").strip(), "new_ip"),
                    ts_port=int((form.get("ts_port", [str(core.DEFAULT_TS_PORT)])[0] or str(core.DEFAULT_TS_PORT)).strip()),
                    supports_qt5_full_status=self._as_bool(form, "supports_qt5_full_status"),
                    enabled=self._as_bool(form, "enabled", default=True),
                    poll_status_every_seconds=float((form.get("poll_status_every_seconds", ["5"])[0] or "5").strip()),
                    poll_full_status_every_seconds=float((form.get("poll_full_status_every_seconds", ["10"])[0] or "10").strip()),
                )
                await self._json(writer, {"ok": True, "result": result})
                return
            if path == "/action/machine-save":
                result = self.app.upsert_loom_config(
                    old_ip=(form.get("old_ip", [""])[0] or "").strip() or None,
                    name=(form.get("name", [""])[0] or "").strip(),
                    new_ip=self._require_text((form.get("new_ip", [""])[0] or "").strip(), "new_ip"),
                    ts_port=int((form.get("ts_port", [str(core.DEFAULT_TS_PORT)])[0] or str(core.DEFAULT_TS_PORT)).strip()),
                    supports_qt5_full_status=self._as_bool(form, "supports_qt5_full_status"),
                    enabled=self._as_bool(form, "enabled", default=True),
                    poll_status_every_seconds=float((form.get("poll_status_every_seconds", ["5"])[0] or "5").strip()),
                    poll_full_status_every_seconds=float((form.get("poll_full_status_every_seconds", ["10"])[0] or "10").strip()),
                )
                await self._json(writer, {"ok": True, "result": result})
                return
            if path == "/action/machine-delete":
                result = self.app.delete_loom_config(self._require_loom(loom_ip))
                await self._json(writer, {"ok": True, "result": result})
                return
            if path == "/action/remote-stop":
                await self._json(writer, {"ok": True, "result": await self.app.remote_stop(self._require_loom(loom_ip))})
                return
            if path == "/action/refresh-all":
                results = []
                for loom in self.app.registry.all_known_looms():
                    if not self.app.is_loom_enabled(loom.ip):
                        continue
                    try:
                        payload = await self.app.query_all_status(loom.ip, force=True)
                        results.append({"loom_ip": loom.ip, "ok": True, "result": payload})
                    except Exception as exc:
                        results.append({"loom_ip": loom.ip, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
                await self._json(writer, {"ok": True, "results": results})
                return
            await self._send_response(writer, 404, "application/json; charset=utf-8", json.dumps({"ok": False, "error": "Not Found"}).encode("utf-8"))
        except Exception as exc:
            await self._json(writer, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

    @staticmethod
    def _require_loom(loom_ip: str) -> str:
        if not loom_ip:
            raise ValueError("loom_ip is required")
        return loom_ip

    @staticmethod
    def _require_text(value: str, name: str) -> str:
        if not value:
            raise ValueError(f"{name} is required")
        return value

    @staticmethod
    def _as_bool(form: Dict[str, List[str]], key: str, default: bool = False) -> bool:
        if key not in form:
            return default
        value = (form.get(key, [""])[0] or "").strip().lower()
        return value in {"1", "true", "yes", "y", "on"}

    async def _json(self, writer: asyncio.StreamWriter, data: Dict[str, Any], status: int = 200) -> None:
        await self._send_response(writer, status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8"))

    @staticmethod
    def _escape(value: Any) -> str:
        return html.escape("" if value is None else str(value))

    @staticmethod
    def _fmt_age(ts: Any) -> str:
        if not ts:
            return "—"
        try:
            delta = max(0.0, core.time.time() - float(ts))
            if delta < 2:
                return "just now"
            if delta < 60:
                return f"{delta:.0f}s"
            if delta < 3600:
                return f"{delta/60:.1f}m"
            return f"{delta/3600:.1f}h"
        except Exception:
            return "—"

    @staticmethod
    def _fmt_efficiency(value: Any) -> str:
        if value in (None, ""):
            return "—"
        try:
            return f"{float(value) / 100:.2f}%"
        except Exception:
            return str(value)

    def _nav(self, current: str) -> str:
        links = [
            ("/", "Dashboard"),
            ("/machines", "Machine IPs"),
            ("/diagnostics", "Diagnostics"),
            ("/live", "Live Events"),
        ]
        items = []
        for href, label in links:
            active = " active" if href == current else ""
            items.append(f'<a class="nav-link{active}" href="{href}">{self._escape(label)}</a>')
        return "".join(items)

    def _brand_logo_svg(self) -> str:
        return """<svg viewBox="0 0 260 90" width="150" height="44" role="img" aria-label="SMIT logo" xmlns="http://www.w3.org/2000/svg" style="display:block;width:150px;height:44px;overflow:visible">
  <text x="0" y="64" fill="#1677C8" font-size="62" font-weight="800" font-family="Arial, Helvetica, sans-serif" letter-spacing="-1">SMIT</text>
</svg>"""

    def _base_page(self, title: str, current: str, body_html: str, scripts: str = "") -> str:
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{self._escape(title)}</title>
  <style>
    :root {{
      --bg:#eef3fb;
      --panel:#ffffff;
      --panel-soft:#f8fbff;
      --line:#dbe5f0;
      --line-strong:#c9d6e6;
      --text:#122033;
      --muted:#6f8097;
      --blue:#3b6cff;
      --purple:#7c4dff;
      --orange:#ff9a1f;
      --green:#1bb35f;
      --red:#ef4c63;
      --chip:#eef4ff;
      --chip-green:#e9fbf1;
      --chip-yellow:#fff4cf;
      --chip-red:#ffe6ea;
      --shadow:0 12px 28px rgba(21, 39, 68, .08);
    }}
    * {{ box-sizing:border-box; }}
    html,body {{ margin:0; padding:0; font-family: Inter, SF Pro Display, Segoe UI, Arial, sans-serif; color:var(--text); background:linear-gradient(180deg,#eef4ff 0%,#f6f9ff 42%,#f2f5fb 100%); }}
    body {{ min-height:100vh; }}
    .wrap {{ max-width: 1600px; margin: 0 auto; padding: 16px 18px 22px; }}
    .topbar {{ display:flex; gap:18px; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; margin-bottom:14px; }}
    .brand {{ display:flex; align-items:flex-start; gap:14px; }}
    .brand-logo-wrap {{ display:flex; align-items:center; justify-content:center; min-width:96px; }}
    .brand-copy {{ display:flex; flex-direction:column; gap:2px; }}
    .brand-copy h1 {{ margin:0; font-size: 27px; letter-spacing:-0.035em; line-height:1.05; }}
    .muted {{ color:var(--muted); font-size:13px; margin-top:4px; }}
    .subhead {{ color:var(--muted); font-size:13px; margin-top:6px; }}
    .meta-note {{ color:var(--muted); font-size:12px; margin-top:3px; opacity:.9; }}
    .nav {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
    .nav-link {{ text-decoration:none; font-size:13px; padding:8px 12px; border-radius:999px; background:rgba(255,255,255,.84); color:var(--text); border:1px solid var(--line); box-shadow:var(--shadow); }}
    .nav-link.active {{ background:linear-gradient(135deg,var(--blue),var(--purple)); color:white; border-color:transparent; }}
    .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; margin:10px 0 14px; }}
    .button {{ appearance:none; border:none; border-radius:12px; padding:9px 13px; min-height:38px; font-weight:700; color:white; background:linear-gradient(135deg,var(--blue),var(--purple)); cursor:pointer; box-shadow:0 10px 20px rgba(47,107,255,.18); font-size:13px; line-height:1.1; }}
    .button:hover {{ filter:brightness(1.04); }}
    .button.read {{ background:linear-gradient(135deg,var(--blue),var(--purple)); }}
    .button.alt {{ background:linear-gradient(135deg,#25c268,var(--green)); box-shadow:0 10px 20px rgba(34,197,94,.16); }}
    .button.warn {{ background:linear-gradient(135deg,#ffa62e,#ff7f11); box-shadow:0 10px 20px rgba(255,143,31,.16); }}
    .button.danger {{ background:linear-gradient(135deg,#ff5f7c,var(--red)); box-shadow:0 10px 20px rgba(239,68,68,.18); }}
    .button.ghost {{ background:#fff; color:var(--text); border:1px solid var(--line); box-shadow:none; }}
    .cards {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(520px,1fr)); gap:16px; }}
    .loom-card {{ background:var(--panel); border:1px solid var(--line); border-radius:24px; padding:14px; box-shadow:var(--shadow); }}
    .card-top {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:8px; }}
    .card-top h2 {{ margin:0; font-size:22px; letter-spacing:-0.035em; line-height:1.05; }}
    .sub {{ color:var(--muted); font-size:13px; margin-top:4px; }}
    .pill {{ border-radius:999px; padding:8px 11px; font-size:12px; font-weight:800; white-space:nowrap; }}
    .pill.ok {{ background:var(--chip-green); color:#127744; }}
    .pill.warn {{ background:var(--chip-yellow); color:#9c6500; }}
    .pill.stop {{ background:var(--chip-red); color:#b4233a; }}
    .banner {{ border-radius:16px; padding:12px 14px; margin-bottom:8px; border:1px solid transparent; }}
    .banner-main {{ font-size:16px; font-weight:900; line-height:1.15; }}
    .banner-sub {{ font-size:12px; color:var(--muted); margin-top:5px; }}
    .banner.running {{ background:linear-gradient(135deg,#dff8e7,#c8f2d6); color:#136b3b; border-color:#bce8ca; }}
    .banner.running .banner-sub {{ color:#3f7258; }}
    .banner.stopped {{ background:linear-gradient(135deg,#ffe7eb,#ffdadd); color:#aa1e3b; border-color:#f5c4cf; }}
    .banner.stopped .banner-sub {{ color:#9b5c69; }}
    .status-strip, .chipline {{ display:flex; flex-wrap:wrap; gap:6px; margin:0 0 10px; }}
    .actions {{ display:grid; gap:8px; margin-bottom:10px; }}
    .action-row {{ display:grid; grid-template-columns:88px repeat(3, minmax(132px, max-content)); gap:10px; align-items:center; }}
    .action-row .button {{ min-width:132px; justify-self:start; }}
    .action-label {{ font-size:11px; font-weight:800; color:var(--muted); text-transform:uppercase; letter-spacing:.12em; }}
    .metrics-grid {{ display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap:8px; margin-bottom:10px; }}
    .metric-tile {{ background:var(--panel-soft); border:1px solid var(--line); border-radius:15px; padding:10px 12px; min-height:78px; }}
    .metric-tile.is-empty {{ background:#fbfcfe; }}
    .metric-label {{ font-size:10px; font-weight:800; color:var(--muted); text-transform:uppercase; letter-spacing:.11em; margin-bottom:6px; }}
    .metric-value {{ font-size:22px; font-weight:900; letter-spacing:-0.04em; line-height:1.05; }}
    .metric-tile.is-empty .metric-value {{ font-size:13px; font-weight:700; color:var(--muted); letter-spacing:0; }}
    .metric-sub {{ font-size:12px; color:var(--muted); margin-top:5px; }}
    .info-grid {{ display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:8px; margin-bottom:8px; }}
    .info-grid.secondary {{ grid-template-columns: repeat(3, minmax(0,1fr)); }}
    .info-card {{ background:#fff; border:1px solid var(--line); border-radius:14px; padding:9px 11px; min-height:62px; }}
    .info-card.secondary {{ background:#fcfdff; }}
    .info-card.tertiary {{ background:#fbfcfe; }}
    .info-card.compact {{ min-height:54px; }}
    .info-card.is-empty .info-value {{ color:var(--muted); font-size:12px; font-style:italic; }}
    .info-label {{ font-size:10px; font-weight:800; color:var(--muted); text-transform:uppercase; letter-spacing:.11em; margin-bottom:5px; }}
    .info-value {{ font-size:14px; line-height:1.3; word-break:break-word; }}
    .admin-strip {{ display:grid; grid-template-columns:2fr 1fr 1fr 1fr 1.15fr; gap:8px; margin:8px 0 6px; }}
    .admin-cell {{ background:#fbfcff; border:1px solid var(--line); border-radius:14px; padding:8px 10px; min-height:58px; }}
    .admin-cell.is-empty .admin-value {{ color:var(--muted); font-size:12px; font-style:italic; }}
    .admin-label {{ font-size:10px; font-weight:800; color:var(--muted); text-transform:uppercase; letter-spacing:.11em; margin-bottom:5px; }}
    .admin-value {{ font-size:14px; line-height:1.25; word-break:break-word; }}
    .chip {{ padding:6px 10px; border-radius:999px; background:var(--chip); border:1px solid #dbe4ff; color:#27407c; font-size:12px; }}
    .chip.good {{ background:var(--chip-green); border-color:#caeedb; color:#137747; }}
    .chip.warn {{ background:var(--chip-yellow); border-color:#f6e3a4; color:#8c6400; }}
    .chip.bad {{ background:var(--chip-red); border-color:#f7c7d0; color:#aa1e3b; }}
    .section {{ background:var(--panel); border:1px solid var(--line); border-radius:24px; padding:16px; box-shadow:var(--shadow); margin-top:18px; }}
    .section h2 {{ margin:0 0 10px 0; font-size:18px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; overflow:hidden; border-radius:16px; border:1px solid var(--line); }}
    th, td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:13px; }}
    th {{ background:#f5f8ff; color:#50617b; font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
    tr:last-child td {{ border-bottom:none; }}
    code, pre {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
    pre {{ white-space:pre-wrap; word-break:break-word; margin:0; font-size:12px; background:#f8fafc; border-radius:12px; padding:10px; border:1px solid var(--line); }}
    .toast {{ position:fixed; right:18px; bottom:18px; border-radius:14px; padding:12px 14px; color:white; max-width:420px; z-index:9999; box-shadow:0 16px 32px rgba(20, 20, 30, .28); opacity:0; transform:translateY(10px); transition:opacity .15s ease, transform .15s ease; }}
    .toast.show {{ opacity:1; transform:translateY(0); }}
    .toast.ok {{ background:linear-gradient(135deg,#16a34a,#22c55e); }}
    .toast.error {{ background:linear-gradient(135deg,#ef4444,#d73c61); }}
    .modal-backdrop {{ position:fixed; inset:0; background:rgba(15,23,42,.38); backdrop-filter: blur(8px); display:none; align-items:center; justify-content:center; z-index:12000; padding:18px; }}
    .modal-backdrop.show {{ display:flex; }}
    .modal-card {{ width:min(520px, 100%); background:rgba(255,255,255,.96); border:1px solid var(--line); border-radius:22px; box-shadow:0 24px 64px rgba(15,23,42,.24); overflow:hidden; }}
    .modal-head {{ padding:16px 18px 10px; border-bottom:1px solid var(--line); }}
    .modal-title {{ margin:0; font-size:18px; letter-spacing:-.02em; }}
    .modal-sub {{ margin-top:4px; color:var(--muted); font-size:13px; }}
    .modal-body {{ padding:16px 18px; display:grid; gap:12px; }}
    .modal-grid {{ display:grid; gap:12px; }}
    .modal-field {{ display:grid; gap:6px; }}
    .modal-field label {{ font-size:12px; font-weight:700; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; }}
    .modal-field input, .modal-field textarea, .modal-field select {{ width:100%; border:1px solid var(--line); border-radius:12px; padding:11px 12px; font:inherit; background:#fff; color:var(--text); }}
    .modal-field textarea {{ min-height:96px; resize:vertical; }}
    .modal-checkbox {{ display:flex; gap:10px; align-items:center; padding:10px 0; }}
    .modal-checkbox input {{ width:18px; height:18px; }}
    .modal-actions {{ display:flex; justify-content:flex-end; gap:10px; padding:14px 18px 18px; border-top:1px solid var(--line); }}
    .modal-note {{ font-size:13px; color:var(--muted); line-height:1.45; }}
    .page-foot {{ display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap; margin-top:14px; color:var(--muted); font-size:12px; opacity:.92; }}
    @media (max-width: 1320px) {{
      .cards {{ grid-template-columns:1fr; }}
      .action-row {{ grid-template-columns:88px repeat(3, minmax(128px, 1fr)); }}
    }}
    @media (max-width: 980px) {{
      .metrics-grid {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
      .info-grid, .info-grid.secondary {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
      .admin-strip {{ grid-template-columns:repeat(2, minmax(0,1fr)); }}
      .action-row {{ grid-template-columns:88px repeat(2, minmax(128px, 1fr)); }}
    }}
    @media (max-width: 760px) {{
      .wrap {{ padding:12px; }}
      .brand {{ gap:10px; }}
      .brand-copy h1 {{ font-size:24px; }}
      .metrics-grid, .info-grid, .info-grid.secondary, .admin-strip {{ grid-template-columns:1fr; }}
      .action-row {{ grid-template-columns:1fr; gap:8px; }}
      .action-row .button {{ width:100%; min-width:unset; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="title-block">
        <div class="brand">
          <div class="brand-logo-wrap">{self._brand_logo_svg()}</div>
          <div class="brand-copy">
            <h1>Loom Real-Time Monitoring Panel</h1>
            <div class="subhead">Live machine dashboard · Beijing time · Generated at <span id="generated-at">{self._escape(bj_now_iso())}</span></div>
            <div class="meta-note">Release {self._escape(RELEASE_VERSION)} · UI improvement release · <span id="session-role">Session role: Monitor</span></div>
          </div>
        </div>
      </div>
      <nav class="nav">{self._nav(current)}</nav>
    </div>
    {body_html}
    <div class="page-foot"><div>Started {self._escape(self.app.started_at_iso)}</div><div>See Diagnostics for config path, logs, runtime health, and communication details.</div></div>
  </div>
  <div id="toast" class="toast"></div>
  <div id="appModal" class="modal-backdrop" aria-hidden="true">
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
      <div class="modal-head">
        <h3 id="modalTitle" class="modal-title">Action</h3>
        <div id="modalSub" class="modal-sub"></div>
      </div>
      <form id="modalForm">
        <div id="modalBody" class="modal-body"></div>
        <div class="modal-actions">
          <button type="button" id="modalCancel" class="button ghost">Cancel</button>
          <button type="submit" id="modalOk" class="button">OK</button>
        </div>
      </form>
    </div>
  </div>
  <script>
    const WRITE_AUTH_DAY_KEY = 'loomWriteAuthDay';
    const modalEl = document.getElementById('appModal');
    const modalTitleEl = document.getElementById('modalTitle');
    const modalSubEl = document.getElementById('modalSub');
    const modalBodyEl = document.getElementById('modalBody');
    const modalFormEl = document.getElementById('modalForm');
    const modalCancelEl = document.getElementById('modalCancel');
    const modalOkEl = document.getElementById('modalOk');
    let modalResolve = null;

    function localDayKey() {{
      const d = new Date();
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      return `${{y}}-${{m}}-${{day}}`;
    }}
    function escHtml(value) {{
      return String(value ?? '').replace(/[&<>"']/g, (m) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m]));
    }}
    function fieldHtml(field) {{
      const name = escHtml(field.name || 'field');
      const label = escHtml(field.label || field.name || 'Field');
      const value = field.value ?? '';
      if (field.type === 'checkbox') {{
        const checked = value ? 'checked' : '';
        return `<div class="modal-checkbox"><input data-field-name="${{name}}" type="checkbox" ${{checked}}><label>${{label}}</label></div>`;
      }}
      if (field.type === 'textarea') {{
        return `<div class="modal-field"><label>${{label}}</label><textarea data-field-name="${{name}}" placeholder="${{escHtml(field.placeholder || '')}}">${{escHtml(value)}}</textarea></div>`;
      }}
      const type = escHtml(field.type || 'text');
      const placeholder = escHtml(field.placeholder || '');
      const autocomplete = escHtml(field.autocomplete || 'off');
      return `<div class="modal-field"><label>${{label}}</label><input data-field-name="${{name}}" type="${{type}}" value="${{escHtml(value)}}" placeholder="${{placeholder}}" autocomplete="${{autocomplete}}"></div>`;
    }}
    function openModal(options = {{}}) {{
      return new Promise((resolve) => {{
        modalResolve = resolve;
        modalTitleEl.textContent = options.title || 'Action';
        modalSubEl.textContent = options.subtitle || '';
        modalOkEl.textContent = options.okText || 'OK';
        modalCancelEl.textContent = options.cancelText || 'Cancel';
        modalOkEl.className = `button${{options.okClass ? ' ' + options.okClass : ''}}`;
        const note = options.message ? `<div class="modal-note">${{escHtml(options.message)}}</div>` : '';
        const fields = (options.fields || []).map(fieldHtml).join('');
        modalBodyEl.innerHTML = `${{note}}<div class="modal-grid">${{fields}}</div>`;
        modalEl.classList.add('show');
        modalEl.setAttribute('aria-hidden', 'false');
        const firstInput = modalBodyEl.querySelector('input, textarea, select');
        if (firstInput) setTimeout(() => firstInput.focus(), 20);
      }});
    }}
    function closeModal(result) {{
      modalEl.classList.remove('show');
      modalEl.setAttribute('aria-hidden', 'true');
      const resolver = modalResolve;
      modalResolve = null;
      if (resolver) resolver(result);
    }}
    modalCancelEl.addEventListener('click', () => closeModal(null));
    modalEl.addEventListener('click', (event) => {{ if (event.target === modalEl) closeModal(null); }});
    modalFormEl.addEventListener('submit', (event) => {{
      event.preventDefault();
      const data = {{}};
      modalBodyEl.querySelectorAll('[data-field-name]').forEach((el) => {{
        const name = el.getAttribute('data-field-name');
        if (!name) return;
        data[name] = el.type === 'checkbox' ? (el.checked ? '1' : '0') : el.value;
      }});
      closeModal(data);
    }});

    async function postForm(path, data, options = {{}}) {{
      const body = new URLSearchParams(data);
      const res = await fetch(path, {{ method:'POST', headers:{{ 'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8' }}, body, credentials:'same-origin' }});
      const payload = await res.json().catch(() => ({{ ok:false, error:'Invalid JSON reply' }}));
      if ((!res.ok || !payload.ok) && res.status === 403 && options.requireWriteAuth) {{
        localStorage.removeItem(WRITE_AUTH_DAY_KEY);
      }}
      if (!res.ok || !payload.ok) throw new Error((payload && payload.error) || `HTTP ${{res.status}}`);
      return payload;
    }}
    async function ensureWriteAuth() {{
      if (localStorage.getItem(WRITE_AUTH_DAY_KEY) === localDayKey()) return;
      const data = await openModal({{
        title: 'Write Access Login',
        subtitle: 'Required once per day for protected write operations.',
        okText: 'Unlock write access',
        fields: [
          {{ name: 'username', label: 'Username', value: 'smit', autocomplete: 'username' }},
          {{ name: 'password', label: 'Password', type: 'password', value: '', autocomplete: 'current-password' }},
        ]
      }});
      if (!data) throw new Error('Write login cancelled');
      await postForm('/auth/login', {{ username: data.username || '', password: data.password || '' }}, {{ requireWriteAuth:false }});
      localStorage.setItem(WRITE_AUTH_DAY_KEY, localDayKey());
      updateSessionRoleBadge();
      showToast('ok', 'Write access enabled for today');
    }}
    async function guardedPostForm(path, data) {{
      await ensureWriteAuth();
      return await postForm(path, data, {{ requireWriteAuth:true }});
    }}
    async function runProtectedSimple(action, loomIp) {{
      try {{
        await guardedPostForm(`/action/${{action}}`, {{ loom_ip: loomIp }});
        showToast('ok', `${{action}} completed for ${{loomIp}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `${{action}} failed for ${{loomIp}}: ${{err.message || err}}`);
      }}
    }}
    function showToast(kind, message) {{
      const toast = document.getElementById('toast');
      toast.className = `toast show ${{kind}}`;
      toast.textContent = message;
      clearTimeout(window.__toastTimer);
      window.__toastTimer = setTimeout(() => {{ toast.className = 'toast'; }}, 3200);
    }}
    function updateSessionRoleBadge() {{
      const el = document.getElementById('session-role');
      if (!el) return;
      const unlocked = localStorage.getItem(WRITE_AUTH_DAY_KEY) === localDayKey();
      el.textContent = unlocked ? 'Session role: Write unlocked' : 'Session role: Monitor';
    }}
    async function runSimple(action, loomIp) {{
      try {{
        await postForm(`/action/${{action}}`, {{ loom_ip: loomIp }});
        showToast('ok', `${{action}} completed for ${{loomIp}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `${{action}} failed for ${{loomIp}}: ${{err.message || err}}`);
      }}
    }}
    async function runReadAll(loomIp) {{
      try {{
        await postForm('/action/read-all', {{ loom_ip: loomIp }});
        showToast('ok', `All status data refreshed for ${{loomIp}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `Read all status failed for ${{loomIp}}: ${{err.message || err}}`);
      }}
    }}

    function readAllStatus(loomIp) {{ return runReadAll(loomIp); }}
    function patternInfo(loomIp) {{ return promptPatternInfo(loomIp); }}

    async function readErrorLogs(loomIp) {{
      try {{
        await postForm('/action/error-logs', {{ loom_ip: loomIp }});
        showToast('ok', `Error logs loaded for ${{loomIp}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `Read error logs failed for ${{loomIp}}: ${{err.message || err}}`);
      }}
    }}
    async function promptAdminDeclaration(loomIp) {{
      const data = await openModal({{
        title: `Administrative declaration · ${{loomIp}}`,
        subtitle: 'Save a declaration code and administrative template. The loom will display it when that declaration code is requested over the TC declaration workflow.',
        okText: 'Save declaration',
        okClass: 'alt',
        fields: [
          {{ name: 'code_str', label: 'Declaration code (3 digits)', value: '565' }},
          {{ name: 'template', label: 'Administrative template', type: 'textarea', value: 'Employee ID: _____ Production Plan ID: _____ Yarn Package Number: _____' }},
        ]
      }});
      if (!data) return;
      try {{
        await guardedPostForm('/action/admin-declaration', {{
          loom_ip: loomIp,
          code_str: data.code_str || '565',
          template: data.template || 'Employee ID: _____ Production Plan ID: _____ Yarn Package Number: _____',
        }});
        showToast('ok', `Administrative declaration saved for ${{loomIp}}. Trigger the same declaration code on the loom HMI to display the form.`);
      }} catch (err) {{
        showToast('error', `Administrative declaration failed for ${{loomIp}}: ${{err.message || err}}`);
      }}
    }}

    async function promptMachineAdd() {{
      const data = await openModal({{
        title: 'Add loom',
        subtitle: 'Add a new loom entry to the current config file.',
        okText: 'Add loom',
        okClass: 'alt',
        fields: [
          {{ name: 'name', label: 'Loom name', value: '2fast-loom-02' }},
          {{ name: 'new_ip', label: 'IP address', value: '169.254.4.102' }},
          {{ name: 'ts_port', label: 'TS port', type: 'number', value: '13000' }},
          {{ name: 'poll_status_every_seconds', label: 'Status poll (s)', type: 'number', value: '2' }},
          {{ name: 'poll_full_status_every_seconds', label: 'Full-status poll (s)', type: 'number', value: '6' }},
          {{ name: 'supports_qt5_full_status', label: 'QT5 full status', type: 'checkbox', value: true }},
          {{ name: 'enabled', label: 'Enabled on dashboard', type: 'checkbox', value: true }},
        ]
      }});
      if (!data) return;
      try {{
        await guardedPostForm('/action/machine-save', {{
          old_ip: '',
          name: data.name || '',
          new_ip: data.new_ip || '',
          ts_port: data.ts_port || '13000',
          poll_status_every_seconds: data.poll_status_every_seconds || '2',
          poll_full_status_every_seconds: data.poll_full_status_every_seconds || '6',
          supports_qt5_full_status: data.supports_qt5_full_status || '0',
          enabled: data.enabled || '0',
        }});
        showToast('ok', `Added loom ${{data.new_ip || ''}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `Add loom failed: ${{err.message || err}}`);
      }}
    }}

    async function promptMachineEdit(oldIp, currentName, currentIp, tsPort, qt5, enabled, pollStatus, pollFull) {{
      const data = await openModal({{
        title: `Edit loom · ${{currentName || currentIp}}`,
        subtitle: 'Update the loom entry saved in the config file.',
        okText: 'Save changes',
        okClass: 'alt',
        fields: [
          {{ name: 'name', label: 'Loom name', value: currentName || '' }},
          {{ name: 'new_ip', label: 'IP address', value: currentIp || '' }},
          {{ name: 'ts_port', label: 'TS port', type: 'number', value: String(tsPort || 13000) }},
          {{ name: 'poll_status_every_seconds', label: 'Status poll (s)', type: 'number', value: String(pollStatus || 2) }},
          {{ name: 'poll_full_status_every_seconds', label: 'Full-status poll (s)', type: 'number', value: String(pollFull || 6) }},
          {{ name: 'supports_qt5_full_status', label: 'QT5 full status', type: 'checkbox', value: !!qt5 }},
          {{ name: 'enabled', label: 'Enabled on dashboard', type: 'checkbox', value: !!enabled }},
        ]
      }});
      if (!data) return;
      try {{
        await guardedPostForm('/action/machine-save', {{
          old_ip: oldIp || '',
          name: data.name || '',
          new_ip: data.new_ip || '',
          ts_port: data.ts_port || '13000',
          poll_status_every_seconds: data.poll_status_every_seconds || '2',
          poll_full_status_every_seconds: data.poll_full_status_every_seconds || '6',
          supports_qt5_full_status: data.supports_qt5_full_status || '0',
          enabled: data.enabled || '0',
        }});
        showToast('ok', `Saved loom ${{data.new_ip || currentIp || ''}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `Edit loom failed: ${{err.message || err}}`);
      }}
    }}

    async function deleteMachine(loomIp, loomName) {{
      const data = await openModal({{
        title: `Delete loom · ${{loomName || loomIp}}`,
        subtitle: 'This removes the loom from the saved config file.',
        okText: 'Delete loom',
        okClass: 'danger',
        message: `Confirm deletion of ${{loomName || loomIp}} (${{loomIp}}).`
      }});
      if (!data) return;
      try {{
        await guardedPostForm('/action/machine-delete', {{ loom_ip: loomIp }});
        showToast('ok', `Deleted loom ${{loomIp}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `Delete loom failed: ${{err.message || err}}`);
      }}
    }}
    async function promptPatternInfo(loomIp) {{
      const data = await openModal({{
        title: `Pattern info · ${{loomIp}}`,
        subtitle: 'Read information for a selected pattern.',
        okText: 'Read pattern info',
        fields: [
          {{ name: 'pattern_name', label: 'Pattern name', value: 'TEST.arm' }},
        ]
      }});
      if (!data || !data.pattern_name) return;
      try {{
        await postForm('/action/pattern-info', {{ loom_ip: loomIp, pattern_name: data.pattern_name }});
        showToast('ok', `Pattern info read for ${{data.pattern_name}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `Pattern info failed: ${{err.message || err}}`);
      }}
    }}
    async function promptPatternSend(loomIp) {{
      const data = await openModal({{
        title: `Send pattern · ${{loomIp}}`,
        subtitle: 'Send a pattern into execution on the loom.',
        okText: 'Send pattern',
        okClass: 'warn',
        fields: [
          {{ name: 'pattern_name', label: 'Pattern name', value: 'TEST.arm' }},
        ]
      }});
      if (!data || !data.pattern_name) return;
      try {{
        await guardedPostForm('/action/pattern-send', {{ loom_ip: loomIp, pattern_name: data.pattern_name }});
        showToast('ok', `Pattern send requested for ${{data.pattern_name}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `Pattern send failed: ${{err.message || err}}`);
      }}
    }}
    async function promptLampWrite(loomIp) {{
      const data = await openModal({{
        title: `Lamp write · ${{loomIp}}`,
        subtitle: 'Enter call numbers such as 1,3 or a raw mask value.',
        okText: 'Write lamp tree',
        okClass: 'warn',
        fields: [
          {{ name: 'calls_mask', label: 'Calls or mask', value: '1' }},
        ]
      }});
      if (!data) return;
      let mask = 0;
      const raw = String(data.calls_mask || '').trim();
      if (/^\d+$/.test(raw) && Number(raw) > 3) {{
        mask = Number(raw);
      }} else {{
        for (const part of raw.split(',')) {{
          const call = Number(part.trim());
          if (call === 1) mask |= 0x80;
          if (call === 2) mask |= 0x40;
          if (call === 3) mask |= 0x20;
        }}
      }}
      try {{
        await guardedPostForm('/action/lamp-write', {{ loom_ip: loomIp, calls_mask: String(mask) }});
        showToast('ok', `Lamp tree written for ${{loomIp}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `Lamp write failed: ${{err.message || err}}`);
      }}
    }}
    async function promptPreselectionWrite(loomIp) {{
      const data = await openModal({{
        title: `Preselection write · ${{loomIp}}`,
        subtitle: 'Update preselection parameters.',
        okText: 'Write preselection',
        okClass: 'warn',
        fields: [
          {{ name: 'enabled', label: 'Enable preselection', type: 'checkbox', value: true }},
          {{ name: 'stop_on_reach', label: 'Stop on value reached', type: 'checkbox', value: true }},
          {{ name: 'confirm_before_restart', label: 'Require confirm before restart', type: 'checkbox', value: false }},
          {{ name: 'delayed_stop', label: 'Delayed stop to optimum cutting position', type: 'checkbox', value: true }},
          {{ name: 'manual_reset', label: 'Manual reset', type: 'checkbox', value: false }},
          {{ name: 'unit', label: 'Unit (1 picks, 2 dm, 3 yards, 4 repeats, 5 metres)', type: 'number', value: '1' }},
          {{ name: 'preselection_value', label: 'Preselection value', type: 'number', value: '1000' }},
          {{ name: 'counter_value', label: 'Counter value', type: 'number', value: '0' }},
        ]
      }});
      if (!data) return;
      try {{
        await guardedPostForm('/action/preselection-write', {{
          loom_ip: loomIp,
          enabled: data.enabled,
          stop_on_reach: data.stop_on_reach,
          confirm_before_restart: data.confirm_before_restart,
          delayed_stop: data.delayed_stop,
          manual_reset: data.manual_reset,
          unit: data.unit || '1',
          preselection_value: data.preselection_value || '0',
          counter_value: data.counter_value || '0',
        }});
        showToast('ok', `Preselection write sent to ${{loomIp}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `Preselection write failed: ${{err.message || err}}`);
      }}
    }}
    async function remoteStop(loomIp) {{
      const data = await openModal({{
        title: `Remote stop · ${{loomIp}}`,
        subtitle: 'This sends operator-stop style remote motion control.',
        okText: 'Send remote stop',
        okClass: 'danger',
        message: 'Confirm that you want to send a remote stop command to this loom.'
      }});
      if (!data) return;
      try {{
        await guardedPostForm('/action/remote-stop', {{ loom_ip: loomIp }});
        showToast('ok', `Remote stop sent to ${{loomIp}}`);
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `Remote stop failed for ${{loomIp}}: ${{err.message || err}}`);
      }}
    }}
    let liveRefreshTimer = null;
    let liveRefreshBusy = false;

    async function liveRefreshDashboard() {{
      if (liveRefreshBusy) return;
      const cardsRoot = document.getElementById('cards-root');
      if (!cardsRoot) return;
      liveRefreshBusy = true;
      try {{
        const res = await fetch('/fragment/dashboard-cards', {{ credentials:'same-origin', cache:'no-store' }});
        const payload = await res.json();
        if (res.ok && payload && payload.ok) {{
          cardsRoot.innerHTML = payload.html || '';
          const gen = document.getElementById('generated-at');
          if (gen && payload.generated_at) gen.textContent = payload.generated_at;
        }}
      }} catch (err) {{
        console.warn('Live dashboard refresh failed', err);
      }} finally {{
        liveRefreshBusy = false;
      }}
    }}

    function startLiveDashboardRefresh() {{
      if (window.location.pathname !== '/') return;
      if (liveRefreshTimer) clearInterval(liveRefreshTimer);
      liveRefreshTimer = setInterval(liveRefreshDashboard, 2000);
    }}

    async function refreshAll() {{
      try {{
        await postForm('/action/refresh-all', {{}});
        showToast('ok', 'All looms refreshed');
        setTimeout(() => liveRefreshDashboard(), 250);
      }} catch (err) {{
        showToast('error', `Refresh failed: ${{err.message || err}}`);
      }}
    }}

    updateSessionRoleBadge();
    startLiveDashboardRefresh();
    {scripts}
  </script>
</body>
</html>"""

    @staticmethod
    def _latest_log_row(logs: Dict[str, Any]) -> Optional[List[str]]:
        if not isinstance(logs, dict):
            return None
        err_rows = (((logs.get("errors") or {}).get("rows")) or [])
        if err_rows:
            return err_rows[-1]
        evt_rows = (((logs.get("events") or {}).get("rows")) or [])
        if evt_rows:
            return evt_rows[-1]
        return None

    @classmethod
    def _latest_log_reason(cls, logs: Dict[str, Any]) -> Optional[str]:
        row = cls._latest_log_row(logs)
        if not row:
            return None
        parts = []
        for idx in (3, 4, 5, 6, 7):
            if idx < len(row):
                value = str(row[idx]).strip()
                if value and value not in {'-', '—'}:
                    parts.append(value)
        if not parts:
            return None
        return ' · '.join(parts[:3])

    def _latest_admin_declaration_event(self, ip: str) -> Optional[Dict[str, Any]]:
        latest = getattr(self.app, 'last_admin_completions', {}).get(ip)
        if latest:
            return latest
        if self.store:
            try:
                for ev in self.store.recent_events(300):
                    if ev.get("peer_ip") == ip and ev.get("event_type") == "declaration_completion":
                        return ev
            except Exception:
                pass
            raw = self._latest_admin_declaration_from_frames(ip)
            if raw:
                return raw
        return None

    def _latest_admin_declaration_from_frames(self, ip: str) -> Optional[Dict[str, Any]]:
        if not self.store:
            return None
        try:
            for fr in self.store.recent_frames(500):
                if fr.get('peer_ip') != ip or fr.get('direction') != 'rx' or int(fr.get('cmd', -1)) != int(core.CMD_DECLARATION):
                    continue
                hexstr = (fr.get('hex') or '').replace(' ', '')
                if not hexstr:
                    continue
                try:
                    raw = bytes.fromhex(hexstr)
                except Exception:
                    continue
                if len(raw) < 2:
                    continue
                payload = raw[2:2+raw[1]]
                if len(payload) <= 3:
                    continue
                try:
                    text = payload.decode('ascii', errors='ignore').rstrip('\x00')
                except Exception:
                    continue
                code = None
                completed = text
                if len(text) >= 3 and text[:3].isdigit():
                    code = text[:3]
                    completed = text[3:]
                else:
                    current_fn = getattr(self.app, 'current_admin_declaration', None)
                    current = current_fn(ip) if callable(current_fn) else None
                    if current and current.get('code_str'):
                        code = current.get('code_str')
                if not completed.strip():
                    continue
                parsed_fields = self._parse_admin_declaration_fields(completed)
                token_like = [t for t in re.split(r'\s+', completed.strip()) if re.fullmatch(r'[A-Za-z0-9_-]+', t)]
                if not parsed_fields and len(token_like) < 3 and not code:
                    continue
                return {
                    'ts_iso': fr.get('ts_iso'),
                    'peer_ip': ip,
                    'event_type': 'declaration_completion',
                    'source': 'TC_PUSH_FRAME_FALLBACK',
                    'payload': {
                        'code': code,
                        'completed_text': completed,
                        'raw_hex': fr.get('hex'),
                    },
                }
        except Exception:
            return None
        return None

    @staticmethod
    def _parse_admin_declaration_fields(text: Any) -> Dict[str, str]:
        src = "" if text is None else str(text)
        compact = src.replace("\r", " ").replace("\n", " ")
        patterns = {
            "employee_id": r"Employee\s*ID\s*:\s*([^\n\r]+?)(?=\s+Production\s*Plan\s*ID\s*:|$)",
            "production_plan_id": r"Production\s*Plan\s*ID\s*:\s*([^\n\r]+?)(?=\s+Yarn\s*Package\s*Number\s*:|$)",
            "yarn_package_number": r"Yarn\s*Package\s*Number\s*:\s*([^\n\r]+)$",
        }
        out: Dict[str, str] = {}
        for key, pat in patterns.items():
            m = re.search(pat, compact, flags=re.IGNORECASE)
            if m:
                out[key] = m.group(1).strip()
        if out:
            return out
        # Fallback: capture the first three numeric/alphanumeric tokens after colons or field separators.
        tokens = [t.strip() for t in re.findall(r':\s*([A-Za-z0-9_-]+)', compact)]
        if len(tokens) >= 3:
            return {
                'employee_id': tokens[0],
                'production_plan_id': tokens[1],
                'yarn_package_number': tokens[2],
            }
        tokens = [t for t in re.split(r'\s+', compact) if t and t not in {'msg:565'}]
        nums = [t for t in tokens if re.fullmatch(r'[A-Za-z0-9_-]+', t)]
        if len(nums) >= 3:
            return {
                'employee_id': nums[-3],
                'production_plan_id': nums[-2],
                'yarn_package_number': nums[-1],
            }
        return out

    def _render_dashboard_cards(self, snapshot: Dict[str, Any]) -> str:
        cards: List[str] = []
        for ip, info in snapshot.items():
            last_status = info.get("last_status") or {}
            event = last_status.get("event") or {}
            interpreted = last_status.get("interpreted") or {}
            commands = info.get("commands") or {}
            speed_result = commands.get("speed") or {}
            picks_result = commands.get("total_picks") or {}
            density_result = commands.get("density") or {}
            pattern_current = commands.get("pattern_current") or {}
            preselection = commands.get("preselection") or {}
            basic_cfg = commands.get("basic_config") or {}
            lamp = commands.get("lamp_tree") or {}
            log_payload = commands.get("error_logs") or {}
            log_reason = self._latest_log_reason(log_payload)
            admin_event = self._latest_admin_declaration_event(ip)
            admin_payload = (admin_event or {}).get("payload") or {}
            admin_text = admin_payload.get("completed_text")
            admin_fields = self._parse_admin_declaration_fields(admin_text)

            running = bool(interpreted.get("running"))
            category = interpreted.get("category") or "unknown"
            detail = interpreted.get("detail")
            if detail in (None, "", "—"):
                detail = "none (running)" if running else "not reported"
            detail_display = detail
            if (not running) and log_reason:
                low_detail = str(detail).lower()
                if low_detail in {"mechanical", "operator_stop", "auxiliary_halt", "unknown_other_stop", "not reported"}:
                    detail_display = f"{detail} · {log_reason}"

            banner_main = "Running" if running else f"{category.replace('_', ' ').title()} / {detail_display.replace('_', ' ')}"
            banner_sub = self._join_parts(
                f"Last update {to_beijing_time_text(last_status.get('updated_at_iso'))}" if last_status.get("updated_at_iso") else "Last update waiting for status",
                f"Source {event.get('source')}" if event.get("source") else "Source not yet available",
            )

            pill_class = "ok" if info.get("tcp_connected") else "warn"
            pill_text = "TC online" if info.get("tcp_connected") else "TC idle"

            speed_value = event.get("speed_rpm")
            if speed_value in (None, ""):
                speed_value = speed_result.get("value")
            picks_value = event.get("total_picks")
            if picks_value in (None, ""):
                picks_value = picks_result.get("value")
            density_value = event.get("density_weft_per_dm")
            if density_value in (None, ""):
                density_value = density_result.get("value")

            metrics = [
                self._metric_card("Speed", speed_value, "rpm"),
                self._metric_card("Picks", picks_value, "total"),
                self._metric_card("Efficiency", self._fmt_efficiency(event.get("efficiency_x100")), "current shift"),
                self._metric_card("Density", density_value, "weft/dm"),
            ]

            primary_infos = [
                self._info_card("Status category", category),
                self._info_card("Status detail", detail_display),
                self._info_card("Current pattern", pattern_current.get("name")),
                self._info_card("Pattern step / colour", self._join_parts(pattern_current.get("step"), pattern_current.get("colour"))),
                self._info_card("Loom type / model", self._join_parts(basic_cfg.get("loom_type"), basic_cfg.get("loom_model"))),
                self._info_card("Software ID / version", basic_cfg.get("software_version")),
            ]

            secondary_infos = [
                self._info_card("Preselection", self._join_parts(preselection.get("preselection_value"), preselection.get("unit_text")), "secondary"),
                self._info_card("Preselection counter", preselection.get("counter_value"), "secondary"),
                self._info_card("TS port", info.get("ts_port", core.DEFAULT_TS_PORT), "secondary"),
                self._info_card("Warp tensions", self._join_parts(event.get("warp_tensions", [None, None, None])[0], event.get("warp_tensions", [None, None, None])[1], event.get("warp_tensions", [None, None, None])[2]), "secondary"),
                self._info_card("Lamp tree mask", lamp.get("mask_hex") or lamp.get("mask"), "tertiary"),
                self._info_card("Last stop / error", log_reason, "tertiary"),
            ]

            admin_infos = [
                self._admin_compact_cell("Last administrative declaration", self._join_parts(admin_payload.get("code_str") or admin_payload.get("code"), (admin_event or {}).get("ts_iso"))),
                self._admin_compact_cell("Employee ID", admin_fields.get("employee_id")),
                self._admin_compact_cell("Production Plan ID", admin_fields.get("production_plan_id")),
                self._admin_compact_cell("Yarn Package Number", admin_fields.get("yarn_package_number")),
                self._admin_compact_cell("Last update", to_beijing_time_text(last_status.get("updated_at_iso")) if last_status.get("updated_at_iso") else None),
            ]

            ts_age = self._fmt_age(info.get("last_ts_poll_ts"))
            tc_age = self._fmt_age(info.get("last_tc_rx_ts"))
            chipline = [
                self._chip(f"TC {'connected' if info.get('tcp_connected') else 'idle'}", "good" if info.get("tcp_connected") else "warn"),
                self._chip(f"TS {ts_age}", "good" if ts_age in {"just now", "1s", "2s"} else "warn"),
                self._chip(f"Last TC packet {tc_age}", "good" if tc_age in {"just now", "1s", "2s"} else "warn"),
                self._chip(f"Declaration {'received' if admin_event else 'waiting'}", "good" if admin_event else "warn"),
            ]
            if basic_cfg.get("serial_number"):
                chipline.append(self._chip(f"Serial {basic_cfg.get('serial_number')}"))
            if density_result.get("unit_text"):
                chipline.append(self._chip(f"Density unit {density_result.get('unit_text')}"))
            if preselection.get("enabled") is not None:
                chipline.append(self._chip(f"Preselection {'ON' if preselection.get('enabled') else 'OFF'}", "good" if preselection.get("enabled") else ""))

            actions = f"""
            <div class=\"actions\">
              <div class=\"action-row\"><div class=\"action-label\">Monitor</div>
                <button class=\"button read\" onclick=\"readAllStatus('{self._escape(ip)}')\">Read All Status</button>
              </div>
              <div class=\"action-row\"><div class=\"action-label\">Pattern</div>
                <button class=\"button read\" onclick=\"patternInfo('{self._escape(ip)}')\">Pattern Info</button>
                <button class=\"button warn\" onclick=\"promptPatternSend('{self._escape(ip)}')\">Send Pattern</button>
              </div>
              <div class=\"action-row\"><div class=\"action-label\">Machine</div>
                <button class=\"button warn\" onclick=\"promptPreselectionWrite('{self._escape(ip)}')\">Preselection Write</button>
                <button class=\"button ghost\" onclick=\"runProtectedSimple('preselection-reset','{self._escape(ip)}')\">Preselection Reset</button>
              </div>
              <div class=\"action-row\"><div class=\"action-label\">Operator</div>
                <button class=\"button alt\" onclick=\"promptAdminDeclaration('{self._escape(ip)}')\">Administrative Declaration</button>
                <button class=\"button warn\" onclick=\"promptLampWrite('{self._escape(ip)}')\">Lamp Write</button>
                <button class=\"button danger\" onclick=\"remoteStop('{self._escape(ip)}')\">Remote Stop</button>
              </div>
            </div>
            """

            cards.append(
                "<section class=\"loom-card\">"
                "<div class=\"card-top\">"
                f"<div><h2>{self._escape(self._display_loom_name(info.get('name') or ip))}</h2><div class=\"sub\">{self._escape(ip)}</div></div>"
                f"<div class=\"pill {pill_class}\">{self._escape(pill_text)}</div>"
                "</div>"
                f"<div class=\"banner {'running' if running else 'stopped'}\"><div class=\"banner-main\">{self._escape(banner_main)}</div><div class=\"banner-sub\">{self._escape(banner_sub)}</div></div>"
                f"<div class=\"status-strip\">{''.join(chipline)}</div>"
                f"{actions}"
                f"<div class=\"metrics-grid\">{''.join(metrics)}</div>"
                f"<div class=\"info-grid\">{''.join(primary_infos)}</div>"
                f"<div class=\"info-grid secondary\">{''.join(secondary_infos)}</div>"
                f"<div class=\"admin-strip\">{''.join(admin_infos)}</div>"
                "</section>"
            )
        return ''.join(cards) if cards else '<section class="loom-card"><h2>No looms configured or seen yet.</h2></section>'

    def _render_dashboard_html(self) -> str:
        snapshot = self.app.dashboard_snapshot()
        cards_html = self._render_dashboard_cards(snapshot)
        body = f"""
        <div class=\"toolbar\">
          <button class=\"button\" onclick=\"refreshAll()\">Refresh all looms now</button>
        </div>
        <div class=\"cards\" id=\"cards-root\">{cards_html}</div>
        """
        return self._base_page("Loom Real-Time Monitoring Panel", "/", body)

    def _render_error_logs_section(self, snapshot: Dict[str, Any]) -> str:
        return ""

    def _render_machines_html(self) -> str:
        cfg_path = self.app.current_config_path() or "Not using an external config file"
        rows: List[str] = []
        for item in self.app.list_loom_configs():
            ip = item.get("ip") or ""
            name = self._display_loom_name(item.get("name") or ip)
            ts_port = int(item.get("ts_port", core.DEFAULT_TS_PORT))
            poll_status = float(item.get("poll_status_every_seconds", 5))
            poll_full = float(item.get("poll_full_status_every_seconds", 10))
            qt5 = bool(item.get("supports_qt5_full_status", False))
            enabled = bool(item.get("enabled", True))
            rows.append(
                "<tr>"
                f"<td>{self._escape(name)}</td>"
                f"<td><code>{self._escape(ip)}</code></td>"
                f"<td>{ts_port}</td>"
                f"<td>{'Yes' if qt5 else 'No'}</td>"
                f"<td>{'Yes' if enabled else 'No'}</td>"
                f"<td><button class=\"button ghost\" type=\"button\" onclick=\"promptMachineEdit('{self._escape(ip)}','{self._escape(name)}','{self._escape(ip)}',{ts_port},{'true' if qt5 else 'false'},{'true' if enabled else 'false'})\">Edit</button> "
                f"<button class=\"button danger\" type=\"button\" onclick=\"deleteMachine('{self._escape(ip)}','{self._escape(name)}')\">Delete</button></td>"
                "</tr>"
            )
        body = f"""
        <section class="section">
          <h2>Machine IP Management</h2>
          <div class="muted">Save loom IPs, names, and TS ports directly into the config file you started the program with.</div>
          <div class="toolbar">
            <button class="button alt" type="button" onclick="promptMachineAdd()">Add loom</button>
            <a class="button ghost" href="/">Back to dashboard</a>
          </div>
          <div class="muted">Config file: {self._escape(cfg_path)}</div>
          <table>
            <thead><tr><th>Name</th><th>IP</th><th>TS Port</th><th>Status Poll(s)</th><th>Full Poll(s)</th><th>QT5 Full Status</th><th>Enabled</th><th>Actions</th></tr></thead>
            <tbody>{''.join(rows) if rows else '<tr><td colspan="8">No looms are configured yet.</td></tr>'}</tbody>
          </table>
        </section>
        """
        return self._base_page("Loom Real-Time Monitoring Panel · Machine IPs", "/machines", body)

    def _render_capabilities_html(self) -> str:
        snapshot = self.app.dashboard_snapshot()
        rows: List[str] = []
        for ip, info in snapshot.items():
            support = info.get("support") or {}
            for key, item in sorted(support.items()):
                rows.append(
                    "<tr>"
                    f"<td>{self._escape(self._display_loom_name(info.get('name') or ip))}</td>"
                    f"<td>{self._escape(ip)}</td>"
                    f"<td>{self._escape(key)}</td>"
                    f"<td>{'Yes' if item.get('ok') else 'No' if item.get('attempts') else '—'}</td>"
                    f"<td>{self._escape(item.get('last_at'))}</td>"
                    f"<td>{self._escape(item.get('summary') or item.get('error') or '—')}</td>"
                    "</tr>"
                )
        body = f"""
        <section class=\"section\">
          <h2>Detected command capabilities</h2>
          <table>
            <thead><tr><th>Loom</th><th>IP</th><th>Command</th><th>Last OK</th><th>Last Tested</th><th>Summary / Error</th></tr></thead>
            <tbody>{''.join(rows) if rows else '<tr><td colspan="6">No command attempts recorded yet.</td></tr>'}</tbody>
          </table>
        </section>
        """
        return self._base_page("Loom Real-Time Monitoring Panel · Capabilities", "/capabilities", body)


    def _render_diagnostics_html(self) -> str:
        diag = self.app.diagnostics_snapshot()
        runtime = diag.get("runtime") or {}
        servers = diag.get("servers") or {}
        looms = diag.get("looms") or []
        rows = []
        for item in looms:
            rows.append(
                "<tr>"
                f"<td>{self._escape(item.get('name'))}</td>"
                f"<td>{self._escape(item.get('ip'))}</td>"
                f"<td>{self._escape(item.get('enabled'))}</td>"
                f"<td>{self._escape(item.get('ts_port'))}</td>"
                f"<td>{self._escape(item.get('poll_status_every_seconds'))}</td>"
                f"<td>{self._escape(item.get('poll_full_status_every_seconds'))}</td>"
                f"<td>{self._escape(item.get('supports_qt5_full_status'))}</td>"
                "</tr>"
            )
        body = f"""
        <section class=\"section\">
          <h2>Runtime diagnostics</h2>
          <table>
            <tbody>
              <tr><th>Release</th><td>{self._escape(runtime.get('release_version'))}</td><th>Date</th><td>{self._escape(runtime.get('release_date'))}</td></tr>
              <tr><th>Started at</th><td>{self._escape(runtime.get('started_at'))}</td><th>Config file</th><td>{self._escape(runtime.get('config_path'))}</td></tr>
              <tr><th>Write actions enabled</th><td>{self._escape(runtime.get('write_actions_enabled'))}</td><th>Log directory</th><td>{self._escape(runtime.get('log_dir'))}</td></tr>
              <tr><th>App log</th><td colspan=\"3\">{self._escape(runtime.get('app_log'))}</td></tr>
              <tr><th>Error log</th><td colspan=\"3\">{self._escape(runtime.get('error_log'))}</td></tr>
              <tr><th>Packaging note</th><td colspan=\"3\">Recommended runtime folders: config/, data/, logs/</td></tr>
            </tbody>
          </table>
        </section>
        <section class=\"section\">
          <h2>Server status</h2>
          <table>
            <thead><tr><th>Layer</th><th>Status</th><th>Listen / Detail</th></tr></thead>
            <tbody>
              <tr><td>TC TCP</td><td>{self._escape((servers.get('tcp') or {}).get('ok'))}</td><td>{self._escape((servers.get('tcp') or {}).get('listen') or (servers.get('tcp') or {}).get('error'))}</td></tr>
              <tr><td>TC UDP</td><td>{self._escape((servers.get('udp') or {}).get('ok'))}</td><td>{self._escape((servers.get('udp') or {}).get('listen') or (servers.get('udp') or {}).get('error'))}</td></tr>
              <tr><td>HTTP</td><td>{self._escape((servers.get('http') or {}).get('ok'))}</td><td>{self._escape((servers.get('http') or {}).get('listen') or (servers.get('http') or {}).get('error'))}</td></tr>
            </tbody>
          </table>
        </section>
        <section class=\"section\">
          <h2>Loom polling configuration</h2>
          <table>
            <thead><tr><th>Name</th><th>IP</th><th>Enabled</th><th>TS Port</th><th>Status Poll(s)</th><th>Full Poll(s)</th><th>QT5 Full Status</th></tr></thead>
            <tbody>{''.join(rows) if rows else '<tr><td colspan="7">No looms configured.</td></tr>'}</tbody>
          </table>
        </section>
        """
        return self._base_page("Loom Real-Time Monitoring Panel · Diagnostics", "/diagnostics", body)

    def _render_live_html(self) -> str:
        tc_event_rows: List[str] = []
        frame_rows: List[str] = []
        if self.store:
            for ev in self.store.recent_events(40):
                src = (ev.get("source") or "").upper()
                if "TC" in src:
                    tc_event_rows.append(
                        "<tr>"
                        f"<td>{self._escape(to_beijing_time_text(ev.get('ts_iso')))}</td>"
                        f"<td>{self._escape(ev.get('peer_ip'))}</td>"
                        f"<td>{self._escape(ev.get('event_type'))}</td>"
                        f"<td><pre>{self._escape(json.dumps(ev.get('payload'), ensure_ascii=False, indent=2))}</pre></td>"
                        "</tr>"
                    )
            for fr in self.store.recent_frames(60):
                if (fr.get("direction") == "rx") and (fr.get("peer_port") == int(self.app.config.get("host_port", core.DEFAULT_HOST_PORT))):
                    frame_rows.append(
                        "<tr>"
                        f"<td>{self._escape(to_beijing_time_text(fr.get('ts_iso')))}</td>"
                        f"<td>{self._escape(fr.get('peer_ip'))}:{self._escape(fr.get('peer_port'))}</td>"
                        f"<td>{self._escape(fr.get('transport'))}</td>"
                        f"<td>{self._escape(fr.get('cmd_hex'))}</td>"
                        f"<td>{self._escape(fr.get('length'))}</td>"
                        f"<td><code>{self._escape(fr.get('frame_hex'))}</code></td>"
                        "</tr>"
                    )
        body = f"""
        <section class=\"section\">
          <h2>TC live decoded events</h2>
          <table>
            <thead><tr><th>Time</th><th>Loom</th><th>Event</th><th>Payload</th></tr></thead>
            <tbody>{''.join(tc_event_rows) if tc_event_rows else '<tr><td colspan="4">No TC decoded events captured yet. This is normal while TC is idle.</td></tr>'}</tbody>
          </table>
        </section>
        <section class=\"section\">
          <h2>Recent inbound TC frames</h2>
          <table>
            <thead><tr><th>Time</th><th>Peer</th><th>Transport</th><th>CMD</th><th>Len</th><th>Frame</th></tr></thead>
            <tbody>{''.join(frame_rows) if frame_rows else '<tr><td colspan="6">No inbound TC frames captured yet.</td></tr>'}</tbody>
          </table>
        </section>
        """
        return self._base_page("Loom Real-Time Monitoring Panel · Live Events", "/live", body)

    def _metric_tile(self, label: str, value: Any, sub: str = "") -> str:
        rendered = "—" if value in (None, "") else self._escape(value)
        sub_html = f'<div class="metric-sub">{self._escape(sub)}</div>' if sub else ''
        return f'<div class="metric-tile"><div class="metric-label">{self._escape(label)}</div><div class="metric-value">{rendered}</div>{sub_html}</div>'

    def _placeholder_text(self, label: str, *, metric: bool = False) -> str:
        key = (label or "").strip().lower()
        if metric:
            if any(token in key for token in ("speed", "picks", "efficiency", "density")):
                return "Not yet read"
            return "—"
        if "administrative declaration" in key:
            return "No declaration received"
        if "employee id" in key or "production plan id" in key or "yarn package number" in key:
            return "Not submitted"
        if "last update" in key:
            return "Waiting for status"
        if "last stop" in key or "error" in key:
            return "No active error"
        if "status detail" in key:
            return "Waiting for status"
        if "status category" in key:
            return "Unknown"
        if any(token in key for token in ("pattern", "software", "loom type", "preselection", "warp", "lamp tree", "ts port")):
            return "Not yet read"
        return "—"

    def _metric_card(self, label: str, value: Any, sub: str = "") -> str:
        empty = value in (None, "")
        rendered = self._escape(self._placeholder_text(label, metric=True) if empty else value)
        empty_cls = " is-empty" if empty else ""
        sub_html = f'<div class="metric-sub">{self._escape(sub)}</div>' if sub else ""
        return f'<div class="metric-tile{empty_cls}"><div class="metric-label">{self._escape(label)}</div><div class="metric-value">{rendered}</div>{sub_html}</div>'

    def _info_card(self, label: str, value: Any, extra_class: str = "") -> str:
        empty = value in (None, "")
        rendered = self._escape(self._placeholder_text(label) if empty else value)
        extra = f" {extra_class}" if extra_class else ""
        empty_cls = " is-empty" if empty else ""
        return f'<div class="info-card{extra}{empty_cls}"><div class="info-label">{self._escape(label)}</div><div class="info-value">{rendered}</div></div>'

    def _admin_compact_cell(self, label: str, value: Any) -> str:
        empty = value in (None, "")
        rendered = self._escape(self._placeholder_text(label) if empty else value)
        empty_cls = " is-empty" if empty else ""
        return f'<div class="admin-cell{empty_cls}"><div class="admin-label">{self._escape(label)}</div><div class="admin-value">{rendered}</div></div>'

    def _chip(self, text: str, tone: str = "") -> str:
        extra = f" {tone}" if tone else ""
        return f'<div class="chip{extra}">{self._escape(text)}</div>'

    @staticmethod
    def _join_parts(*parts: Any) -> str:
        cleaned = [str(p) for p in parts if p not in (None, "", [], {})]
        return " · ".join(cleaned) if cleaned else "—"


class MasterApp(core.LoomHostApp):
    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.dashboard = InteractiveDashboardServer(self, self.registry, self.sink, self.store, self.log)
        self.command_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.command_support: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.admin_declarations: Dict[str, Dict[str, Any]] = {}
        self.last_admin_completions: Dict[str, Dict[str, Any]] = {}
        self.runtime_by_loom: Dict[str, Dict[str, Any]] = {}
        _orig_log_event = self.sink.log_event
        def _wrapped_log_event(peer_ip: str, event_type: str, source: str, payload: Dict[str, Any]) -> None:
            if event_type == 'declaration_completion':
                self.last_admin_completions[peer_ip] = {
                    'ts_iso': bj_now_iso(),
                    'peer_ip': peer_ip,
                    'event_type': event_type,
                    'source': source,
                    'payload': dict(payload or {}),
                }
            if event_type in ('status', 'complete_status', 'full_status'):
                try:
                    self._update_runtime_from_event(peer_ip, payload or {}, source=source, event_type=event_type)
                except Exception:
                    self.log.exception("Runtime tracking failed for %s event=%s", peer_ip, event_type)
            _orig_log_event(peer_ip, event_type, source, payload)
        self.sink.log_event = _wrapped_log_event
        self.started_at = time.time()
        self.startup_status: Dict[str, Any] = {"tcp": None, "udp": None, "http": None}


    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            num = float(value)
            if num != num:
                return None
            return num
        except Exception:
            return None

    @staticmethod
    def _coerce_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return bool(value)
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "running", "run", "on"):
            return True
        if s in ("0", "false", "no", "stopped", "stop", "off", "idle", "halt", "halted", "generic_stop"):
            return False
        if "stop" in s or "halt" in s or s.endswith("_off"):
            return False
        return None

    def _normalize_speed_rpm(self, value: Any) -> Optional[float]:
        """Normalize speed returned by the loom protocol.

        This only handles numeric values that are already decoded from the correct
        protocol bytes. Byte-level ambiguity is handled by _decode_speed_bytes().
        """
        speed = self._coerce_float(value)
        if speed is None:
            return None
        abs_speed = abs(speed)
        if abs_speed > 10000:
            speed = speed / 100.0
        elif abs_speed > 2500:
            candidate = speed / 100.0
            if 0 <= abs(candidate) <= 2500:
                speed = candidate
        return round(float(speed), 1)

    def _decode_speed_bytes(self, speed_bytes: bytes) -> Tuple[Optional[float], Dict[str, Any]]:
        """Decode speed bytes from a dedicated speed reply.

        The latest field test showed a payload pattern where bytes C8 00 represent
        approximately 200 rpm. Treating those two bytes as a big-endian integer
        gives 51200, which then becomes 512.0 after x100 scaling and is wrong.

        Selection rule:
        - [speed, 0x00] or [0x00, speed] in a realistic compact range is direct rpm.
        - otherwise use big-endian word, applying x100 scaling only when plausible.
        """
        if not speed_bytes or len(speed_bytes) < 2:
            return None, {"mode": "invalid", "raw_bytes": bytes(speed_bytes or b'').hex().upper()}
        b0 = int(speed_bytes[0]) & 0xFF
        b1 = int(speed_bytes[1]) & 0xFF
        raw_word = core.u16(b0, b1)
        debug = {
            "raw_bytes": bytes([b0, b1]).hex().upper(),
            "high_byte": b0,
            "low_byte": b1,
            "big_endian_word": raw_word,
        }

        if b1 == 0 and 0 < b0 <= 250:
            debug["mode"] = "single_byte_high_direct_rpm"
            return round(float(b0), 1), debug
        if b0 == 0 and 0 <= b1 <= 250:
            debug["mode"] = "single_byte_low_direct_rpm"
            return round(float(b1), 1), debug

        rpm = self._normalize_speed_rpm(raw_word)
        debug["mode"] = "big_endian_word"
        debug["normalized_from_word"] = rpm
        return rpm, debug

    def _runtime_state_for(self, loom_ip: str) -> Dict[str, Any]:
        return self.runtime_by_loom.setdefault(loom_ip, {
            "loom_ip": loom_ip,
            "running": False,
            "current_start_ts": None,
            "current_start_iso": None,
            "last_update_ts": None,
            "last_update_iso": None,
            "last_speed_rpm": None,
            "speed_history": [],
            "last_bucket_second": -1,
        })

    def _extract_runtime_signal(self, payload: Dict[str, Any]) -> Tuple[Optional[bool], Optional[float]]:
        event = payload.get("event") or {}
        interpreted = payload.get("interpreted") or {}
        running = self._coerce_bool(interpreted.get("running"))
        if running is None:
            running = self._coerce_bool(event.get("running"))
        if running is None:
            running = self._coerce_bool(interpreted.get("category"))
        speed = self._coerce_float(event.get("speed_rpm"))
        if speed is None:
            speed = self._coerce_float(payload.get("speed_rpm"))
        speed = self._normalize_speed_rpm(speed)
        return running, speed

    def _update_runtime_history(
        self,
        loom_ip: str,
        *,
        running: Optional[bool],
        speed_rpm: Optional[float] = None,
        ts: Optional[float] = None,
        source: str = "",
    ) -> Dict[str, Any]:
        now_ts = float(ts if ts is not None else time.time())
        state = self._runtime_state_for(loom_ip)

        if running is None and speed_rpm is not None:
            try:
                if float(speed_rpm) > 0:
                    running = True
            except Exception:
                pass

        if running is False:
            state.update({
                "running": False,
                "current_start_ts": None,
                "current_start_iso": None,
                "last_update_ts": now_ts,
                "last_update_iso": to_beijing_time_text(now_ts),
                "last_bucket_second": -1,
                "speed_history": [],
            })
            return self.runtime_snapshot(loom_ip)

        if running is True:
            if not state.get("running") or not state.get("current_start_ts"):
                state.update({
                    "running": True,
                    "current_start_ts": now_ts,
                    "current_start_iso": to_beijing_time_text(now_ts),
                    "speed_history": [],
                    "last_bucket_second": -1,
                })
            else:
                state["running"] = True

        active = bool(state.get("running") and state.get("current_start_ts"))
        state["last_update_ts"] = now_ts
        state["last_update_iso"] = to_beijing_time_text(now_ts)

        if speed_rpm is not None:
            speed_rpm = self._normalize_speed_rpm(speed_rpm)

        if speed_rpm is not None:
            state["last_speed_rpm"] = float(speed_rpm)
            if active:
                elapsed_s = max(0.0, now_ts - float(state["current_start_ts"]))
                bucket_second = max(0, int(elapsed_s))
                bucket_minute = max(1, int(elapsed_s // 60) + 1)
                point = {
                    "second": bucket_second,
                    "minute": bucket_minute,
                    "speed_rpm": round(float(speed_rpm), 1),
                    "source": source,
                }
                history = list(state.get("speed_history") or [])
                if history and int(history[-1].get("second", -1)) == bucket_second:
                    history[-1] = point
                else:
                    history.append(point)
                # Keep enough one-second samples for long real tests without persisting to disk.
                state["speed_history"] = history[-7200:]
                state["last_bucket_second"] = bucket_second

        return self.runtime_snapshot(loom_ip)

    def _update_runtime_from_event(self, loom_ip: str, payload: Dict[str, Any], *, source: str = "", event_type: str = "") -> Dict[str, Any]:
        running, speed = self._extract_runtime_signal(payload)
        label = f"{event_type}:{source}".strip(':')
        return self._update_runtime_history(loom_ip, running=running, speed_rpm=speed, source=label)

    def _runtime_from_query_result(self, loom_ip: str, result: Dict[str, Any]) -> Dict[str, Any]:
        commands = result.get("commands") or {}
        status_payload = commands.get("status") or {}
        status_decoded = status_payload.get("decoded") or {}
        status_event = status_decoded.get("event") or {}
        status_interpreted = status_decoded.get("interpreted") or {}
        full_payload = commands.get("full_status") or {}
        full_decoded = full_payload.get("decoded") or {}
        full_event = full_decoded.get("event") or {}
        full_interpreted = full_decoded.get("interpreted") or {}
        speed_payload = commands.get("speed") or {}

        interpreted = full_interpreted or status_interpreted
        running = self._coerce_bool(interpreted.get("running"))
        if running is None:
            running = self._coerce_bool(interpreted.get("category"))
        speed = self._coerce_float(full_event.get("speed_rpm"))
        if speed is None:
            speed = self._coerce_float(status_event.get("speed_rpm"))
        if speed is None:
            speed = self._coerce_float(speed_payload.get("speed_rpm"))
        return self._update_runtime_history(loom_ip, running=running, speed_rpm=speed, source="read_all")

    def runtime_snapshot(self, loom_ip: str) -> Dict[str, Any]:
        state = self._runtime_state_for(loom_ip)
        now_ts = time.time()
        running = bool(state.get("running") and state.get("current_start_ts"))
        elapsed_s = max(0.0, now_ts - float(state["current_start_ts"])) if running else 0.0
        runtime_minutes = int(elapsed_s // 60) + 1 if running else 0
        history = list(state.get("speed_history") or [])
        return {
            "loom_ip": loom_ip,
            "running": running,
            "currentStartIso": state.get("current_start_iso"),
            "lastUpdateIso": state.get("last_update_iso"),
            "runtimeSeconds": int(round(elapsed_s)),
            "runtimeMinutes": runtime_minutes,
            "lastSpeedRpm": state.get("last_speed_rpm"),
            "speedHistory": history,
            "speedHistoryValues": [p.get("speed_rpm") for p in history if p.get("speed_rpm") is not None],
        }

    def current_admin_completion(self, loom_ip: str) -> Dict[str, Any]:
        event = self.last_admin_completions.get(loom_ip)
        if not event:
            try:
                event = self.dashboard._latest_admin_declaration_event(loom_ip)
            except Exception:
                event = None
        if not event:
            return {"hasData": False, "loom_ip": loom_ip}
        payload = dict(event.get("payload") or {})
        completed_text = (
            payload.get("completed_text")
            or payload.get("completedText")
            or payload.get("reply")
            or payload.get("text")
            or payload.get("raw_text")
            or ""
        )
        fields = payload.get("fields") or payload.get("parsed_fields") or {}
        if not fields and completed_text:
            try:
                fields = InteractiveDashboardServer._parse_admin_declaration_fields(completed_text)
            except Exception:
                fields = {}
        return {
            "hasData": bool(completed_text or fields),
            "loom_ip": loom_ip,
            "code": payload.get("code") or payload.get("code_str"),
            "receivedAt": event.get("ts_iso") or event.get("updated_at_iso") or bj_now_iso(),
            "ts_iso": event.get("ts_iso") or event.get("updated_at_iso") or bj_now_iso(),
            "completedText": completed_text,
            "fields": fields,
            "source": event.get("source"),
        }

    async def query_realtime_status(self, loom_ip: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {"loom_ip": loom_ip, "updated_at_iso": bj_now_iso()}
        try:
            speed = await self.query_speed(loom_ip)
            result["speed"] = speed
            result["currentSpeed"] = speed.get("speed_rpm")
        except Exception as exc:
            result["speed"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        runtime = self.runtime_snapshot(loom_ip)
        result["runtime"] = runtime
        result["runtimeMinutes"] = runtime.get("runtimeMinutes", 0)
        result["runtimeSeconds"] = runtime.get("runtimeSeconds", 0)
        result["speedHistory"] = runtime.get("speedHistoryValues") or runtime.get("speedHistory", [])
        result["adminCompletion"] = self.current_admin_completion(loom_ip)
        return result


    def current_config_path(self) -> Optional[str]:
        return self.config.get("__config_path")

    @property
    def started_at_iso(self) -> str:
        ts = getattr(self, "started_at", time.time())
        return to_beijing_time_text(ts)

    def log_paths(self) -> Dict[str, Any]:
        return dict(self.config.get("__log_info") or {})

    def diagnostics_snapshot(self) -> Dict[str, Any]:
        return {
            "runtime": {
                "release_version": RELEASE_VERSION,
                "release_date": RELEASE_DATE,
                "started_at": self.started_at_iso,
                "config_path": self.current_config_path() or "in-memory",
                "write_actions_enabled": bool(self.config.get("write_actions_enabled", True)),
                "log_dir": self.log_paths().get("log_dir", "logs"),
                "app_log": self.log_paths().get("app_log", ""),
                "error_log": self.log_paths().get("error_log", ""),
            },
            "servers": dict(getattr(self, "startup_status", {})),
            "looms": self.list_loom_configs(),
        }


    def list_loom_configs(self) -> List[Dict[str, Any]]:
        items = []
        for item in self.config.get("looms", []):
            row = dict(item)
            row.setdefault("enabled", True)
            row.setdefault("poll_status_every_seconds", 5)
            row.setdefault("poll_full_status_every_seconds", 10)
            items.append(row)
        return items

    def is_loom_enabled(self, loom_ip: str) -> bool:
        cfg = self.registry.config_by_ip.get(loom_ip) or {}
        return bool(cfg.get("enabled", True))

    def _write_runtime_config(self) -> None:
        config_path = self.current_config_path()
        if not config_path:
            raise ValueError("No external config file is active; start the program with --config to enable saving.")
        path = Path(config_path)
        payload = dict(self.config)
        payload.pop("__config_path", None)
        payload.pop("__log_info", None)
        backup_dir = Path(self.config.get("config_backup_dir", path.parent / "config_backups"))
        backup_dir.mkdir(parents=True, exist_ok=True)
        if path.exists():
            stamp = datetime.now(BEIJING_TZ).strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"{path.stem}_{stamp}{path.suffix}.bak"
            shutil.copy2(path, backup_path)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def upsert_loom_config(
        self,
        old_ip: Optional[str],
        name: str,
        new_ip: str,
        ts_port: int,
        supports_qt5_full_status: bool,
        enabled: bool = True,
        poll_status_every_seconds: float = 5.0,
        poll_full_status_every_seconds: float = 10.0,
    ) -> Dict[str, Any]:
        new_ip = str(new_ip or "").strip()
        if not new_ip:
            raise ValueError("new_ip is required")
        if ts_port <= 0 or ts_port > 65535:
            raise ValueError("ts_port must be between 1 and 65535")
        if poll_status_every_seconds <= 0.2:
            raise ValueError("poll_status_every_seconds must be > 0.2")
        if poll_full_status_every_seconds < 6.0:
            poll_full_status_every_seconds = 6.0
        name = (name or "").strip() or f"loom-{new_ip}"
        looms = self.config.setdefault("looms", [])

        existing_idx = None
        old_item = None
        if old_ip:
            for idx, item in enumerate(looms):
                if str(item.get("ip")) == old_ip:
                    existing_idx = idx
                    old_item = dict(item)
                    break

        # prevent duplicate IP when editing/adding
        for idx, item in enumerate(looms):
            if str(item.get("ip")) == new_ip and idx != existing_idx:
                raise ValueError(f"A loom with IP {new_ip} already exists")

        updated = dict(old_item or {})
        updated.update({
            "name": name,
            "ip": new_ip,
            "ts_port": int(ts_port),
            "poll_status_every_seconds": float(poll_status_every_seconds),
            "poll_full_status_every_seconds": float(poll_full_status_every_seconds),
            "supports_qt5_full_status": bool(supports_qt5_full_status),
            "enabled": bool(enabled),
        })
        updated.setdefault("declarations", (old_item or {}).get("declarations", {}))

        if existing_idx is None:
            looms.append(updated)
        else:
            looms[existing_idx] = updated

        # update runtime config mappings
        if old_ip and old_ip != new_ip:
            self.registry.config_by_ip.pop(old_ip, None)
            self.command_cache[new_ip] = self.command_cache.pop(old_ip, {})
            self.command_support[new_ip] = self.command_support.pop(old_ip, {})
            self.admin_declarations[new_ip] = self.admin_declarations.pop(old_ip, {})
            self.last_admin_completions[new_ip] = self.last_admin_completions.pop(old_ip, {})
            self.runtime_by_loom[new_ip] = self.runtime_by_loom.pop(old_ip, {})
        self.registry.config_by_ip[new_ip] = updated

        self._write_runtime_config()
        return {"ok": True, "loom": updated, "config_path": self.current_config_path()}

    def delete_loom_config(self, loom_ip: str) -> Dict[str, Any]:
        loom_ip = str(loom_ip or "").strip()
        if not loom_ip:
            raise ValueError("loom_ip is required")
        looms = self.config.setdefault("looms", [])
        new_looms = [item for item in looms if str(item.get("ip")) != loom_ip]
        if len(new_looms) == len(looms):
            raise ValueError(f"No configured loom found for IP {loom_ip}")
        self.config["looms"] = new_looms
        self.registry.config_by_ip.pop(loom_ip, None)
        self.command_cache.pop(loom_ip, None)
        self.command_support.pop(loom_ip, None)
        self.admin_declarations.pop(loom_ip, None)
        self.last_admin_completions.pop(loom_ip, None)
        self.runtime_by_loom.pop(loom_ip, None)
        self._write_runtime_config()
        return {"ok": True, "deleted_ip": loom_ip, "config_path": self.current_config_path()}

    def configure_admin_declaration(self, loom_ip: str, code_str: str, template: str) -> Dict[str, Any]:
        code = str(code_str or '').strip()
        if not re.fullmatch(r"\d{3}", code):
            raise ValueError("code_str must be exactly 3 digits")
        tpl = str(template or '').strip()
        if not tpl:
            raise ValueError("template is required")
        cfg = self.registry.config_by_ip.setdefault(loom_ip, {"name": f"unknown-{loom_ip}", "ip": loom_ip, "declarations": {}})
        decls = cfg.setdefault("declarations", {})
        decls[code] = tpl
        payload = {"code_str": code, "template": tpl, "updated_at_iso": bj_now_iso()}
        self.admin_declarations[loom_ip] = payload
        self._remember(loom_ip, "admin_declaration", {"loom_ip": loom_ip, **payload}, summary=f"Administrative declaration {code} configured")
        self.log.info("Administrative declaration configured for %s: code=%s", loom_ip, code)
        return payload

    def current_admin_declaration(self, loom_ip: str) -> Optional[Dict[str, Any]]:
        current = self.admin_declarations.get(loom_ip)
        if current:
            return current
        cmd = (self.command_cache.get(loom_ip, {}) or {}).get('admin_declaration') or {}
        if cmd.get('code_str'):
            return cmd
        return None

    def resolve_declaration(self, ip: str, code_str: str) -> Optional[str]:
        template = self.registry.declaration_reply_for(ip, code_str)
        if template is None:
            return None
        if template.startswith(code_str):
            return template
        return f"{code_str}{template}"

    # ----- generic helpers -----
    def _port_for(self, loom_ip: str) -> int:
        return int(self.registry.config_by_ip.get(loom_ip, {}).get("ts_port", core.DEFAULT_TS_PORT))

    async def _request(self, loom_ip: str, request: bytes) -> core.Frame:
        return await self.ts_client.transact(loom_ip, self._port_for(loom_ip), request)

    def _remember(self, loom_ip: str, key: str, result: Dict[str, Any], *, ok: bool = True, summary: Optional[str] = None, error: Optional[str] = None) -> None:
        by_ip = self.command_cache.setdefault(loom_ip, {})
        support = self.command_support.setdefault(loom_ip, {})
        item = dict(result)
        item["ok"] = ok
        item["updated_at_iso"] = bj_now_iso()
        item["updated_at_ts"] = core.time.time()
        by_ip[key] = item

        supp = support.setdefault(key, {"attempts": 0, "ok": False})
        supp["attempts"] = int(supp.get("attempts", 0)) + 1
        supp["ok"] = ok
        supp["last_at"] = bj_now_iso()
        supp["summary"] = summary or ("OK" if ok else None)
        if error:
            supp["error"] = error
        elif ok:
            supp.pop("error", None)

    def _wrap_error(self, loom_ip: str, key: str, exc: Exception) -> None:
        self._remember(loom_ip, key, {"error": f"{type(exc).__name__}: {exc}"}, ok=False, error=f"{type(exc).__name__}: {exc}")

    def dashboard_snapshot(self) -> Dict[str, Any]:
        base = self.registry.snapshot()
        merged: Dict[str, Any] = {}
        for ip, item in base.items():
            if not self.is_loom_enabled(ip):
                continue
            merged[ip] = dict(item)
            merged[ip]["commands"] = self.command_cache.get(ip, {})
            merged[ip]["support"] = self.command_support.get(ip, {})
            merged[ip]["runtime"] = self.runtime_snapshot(ip)
        for ip in self.command_cache:
            if not self.is_loom_enabled(ip):
                continue
            if ip not in merged:
                merged[ip] = {
                    "name": ip,
                    "ip": ip,
                    "ts_port": self._port_for(ip),
                    "tcp_connected": False,
                    "current_peer_port": None,
                    "last_tc_rx_ts": 0.0,
                    "last_ts_poll_ts": 0.0,
                    "last_status": None,
                    "supports_qt5_full_status": False,
                    "commands": self.command_cache.get(ip, {}),
                    "support": self.command_support.get(ip, {}),
                    "runtime": self.runtime_snapshot(ip),
                }
        return merged

    # ----- built-in original commands -----
    async def query_status(self, loom_ip: str) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, core.encode_status_request())
            result: Dict[str, Any] = {"loom_ip": loom_ip, "cmd": frame.cmd, "length": frame.length, "raw_hex": frame.as_hex()}
            if frame.cmd == core.CMD_STATUS:
                ev = core.decode_status(frame.payload, source="TS_REPLY")
                interpreted = core.interpret_status(ev)
                self.registry.set_last_status(loom_ip, ev)
                self.sink.log_event(loom_ip, "status", "TS_REPLY", {"event": ev.__dict__, "interpreted": interpreted})
                result["decoded"] = {"event": ev.__dict__, "interpreted": interpreted}
            self._remember(loom_ip, "status", result, summary=f"Status len={frame.length}")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "status", exc)
            raise

    async def query_full_status(self, loom_ip: str) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, core.encode_full_status_request())
            result: Dict[str, Any] = {"loom_ip": loom_ip, "cmd": frame.cmd, "length": frame.length, "raw_hex": frame.as_hex()}
            if frame.cmd == core.CMD_FULL_STATUS:
                ev = core.decode_full_status(frame.payload, source="TS_REPLY")
                interpreted = core.interpret_status(ev)
                self.registry.set_last_status(loom_ip, ev)
                self.sink.log_event(loom_ip, "full_status", "TS_REPLY", {"event": ev.__dict__, "interpreted": interpreted})
                result["decoded"] = {"event": ev.__dict__, "interpreted": interpreted}
            self._remember(loom_ip, "full_status", result, summary=f"Full status len={frame.length}")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "full_status", exc)
            raise


    def _command_age(self, loom_ip: str, key: str) -> float:
        item = (self.command_cache.get(loom_ip, {}) or {}).get(key, {}) or {}
        ts = item.get("updated_at_ts")
        if ts in (None, ""):
            return 1e9
        try:
            return max(0.0, core.time.time() - float(ts))
        except Exception:
            return 1e9

    async def query_all_status(self, loom_ip: str, force: bool = False) -> Dict[str, Any]:
        result: Dict[str, Any] = {"loom_ip": loom_ip, "updated_at_iso": bj_now_iso(), "commands": {}}

        async def run(key: str, coro_factory, min_age: float = 0.0) -> Dict[str, Any]:
            if (not force) and self._command_age(loom_ip, key) < min_age:
                cached = dict((self.command_cache.get(loom_ip, {}) or {}).get(key, {}) or {})
                result["commands"][key] = cached
                return cached
            try:
                payload = await coro_factory(loom_ip)
                result["commands"][key] = payload
                return payload
            except Exception as exc:
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                result["commands"][key] = payload
                return payload

        status_payload = await run("status", self.query_status, 1.0)
        status_interp = (((status_payload.get("decoded") or {}).get("interpreted")) or {}) if isinstance(status_payload, dict) else {}
        full_payload = await run("full_status", self.query_full_status, 6.0)
        full_event = (((full_payload.get("decoded") or {}).get("event")) or {}) if isinstance(full_payload, dict) else {}
        if full_payload.get("error") or full_event.get("speed_rpm") in (None, ""):
            await run("speed", self.query_speed, 0.5)
        await run("total_picks", self.query_total_picks, 2.0)
        await run("density", self.query_density, 2.0)
        await run("pattern_current", self.query_pattern_current, 2.0)
        await run("basic_config", self.query_basic_config, 2.0)
        await run("preselection", self.query_preselection, 2.0)
        await run("lamp_tree", self.query_lamp_tree, 2.0)
        if force or not bool(status_interp.get("running")):
            await run("error_logs", self.query_error_logs, 10.0)
        runtime = self._runtime_from_query_result(loom_ip, result)
        result["runtime"] = runtime
        result["runtimeMinutes"] = runtime.get("runtimeMinutes", 0)
        result["runtimeSeconds"] = runtime.get("runtimeSeconds", 0)
        result["speedHistory"] = runtime.get("speedHistory", [])
        result["speedHistoryValues"] = runtime.get("speedHistoryValues", [])
        return result

    async def refresh_dashboard(self, force: bool = False) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        for loom in self.registry.all_known_looms():
            if not self.is_loom_enabled(loom.ip):
                continue
            try:
                results[loom.ip] = await self.query_all_status(loom.ip, force=force)
            except Exception as exc:
                results[loom.ip] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return results

    async def send_popup(self, loom_ip: str, message: str) -> None:
        try:
            frame = await self._request(loom_ip, core.encode_popup_message(message))
            self._remember(loom_ip, "popup", {"loom_ip": loom_ip, "message": message, "reply_cmd": frame.cmd, "reply_len": frame.length, "raw_hex": frame.as_hex()}, summary="Popup sent")
            self.log.info("Popup transaction completed to %s, reply cmd=0x%02X len=%d", loom_ip, frame.cmd, frame.length)
        except Exception as exc:
            self._wrap_error(loom_ip, "popup", exc)
            raise

    async def send_raw_hex(self, loom_ip: str, hex_string: str) -> Dict[str, Any]:
        cleaned = "".join(ch for ch in hex_string if ch not in " \t\r\n:-")
        request = bytes.fromhex(cleaned)
        frame = await self._request(loom_ip, request)
        return {"loom_ip": loom_ip, "request_hex": request.hex().upper(), "reply_hex": frame.as_hex(), "reply_cmd": frame.cmd, "reply_len": frame.length}

    # ----- additional protocol helpers -----
    @staticmethod
    def _ensure_ascii_24(name: str) -> bytes:
        raw = name.encode("ascii", errors="strict")
        if len(raw) > 24:
            raise ValueError("pattern name must be <= 24 ASCII bytes")
        return raw.ljust(24, b"\x00")

    @staticmethod
    def _u32_from_payload(payload: bytes) -> int:
        if len(payload) != 4:
            raise core.ProtocolError(f"expected 4 bytes, got {len(payload)}")
        return core.u32(payload[0], payload[1], payload[2], payload[3])

    @staticmethod
    def _read_result_payload(payload: bytes, *, expected_res: Optional[int], min_data_len: int = 0) -> Tuple[Optional[int], bytes]:
        if expected_res is not None and payload and payload[0] == expected_res and len(payload) - 1 >= min_data_len:
            return payload[0], payload[1:]
        if payload and payload[0] in RESULT_TEXT and len(payload) - 1 >= min_data_len:
            return payload[0], payload[1:]
        return None, payload

    async def query_speed(self, loom_ip: str) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, core.encode_speed_request())
            if frame.cmd != core.CMD_SPEED:
                raise core.ProtocolError(f"invalid speed reply cmd=0x{frame.cmd:02X} len={frame.length}")
            if frame.length < 2:
                raise core.ProtocolError(f"speed reply too short len={frame.length}")

            payload = frame.payload
            notes = []
            extra_hex = None

            # The PDF says MAC 50 2 HIGH_SPEED LOW_SPEED, but some looms have been
            # observed returning 3 payload bytes. In that case, prefer the last two bytes
            # when the first byte looks like an extra result/status byte.
            if frame.length == 2:
                speed_bytes = payload[:2]
            elif frame.length == 3:
                if payload[0] in RESULT_TEXT or payload[0] in (0x00, 0x01, 0x55):
                    speed_bytes = payload[1:3]
                    notes.append('3-byte speed reply; used trailing 2 bytes')
                else:
                    speed_bytes = payload[:2]
                    notes.append('3-byte speed reply; used first 2 bytes')
                extra_hex = payload.hex().upper()
            else:
                speed_bytes = payload[:2]
                extra_hex = payload.hex().upper()
                notes.append(f'non-standard speed reply len={frame.length}; used first 2 bytes')

            raw_speed = core.u16(speed_bytes[0], speed_bytes[1])
            rpm, speed_debug = self._decode_speed_bytes(bytes(speed_bytes[:2]))
            result = {
                "loom_ip": loom_ip,
                "speed_rpm": rpm,
                "raw_speed_value": raw_speed,
                "speed_decode": speed_debug,
                "raw_hex": frame.as_hex(),
            }
            if rpm != raw_speed:
                notes.append(f'raw speed word {raw_speed} decoded to {rpm} rpm using {speed_debug.get("mode")}')
            if extra_hex is not None:
                result["payload_hex"] = extra_hex
            if notes:
                result["notes"] = notes
            self._remember(loom_ip, "speed", result, summary=f"Speed {rpm} rpm")
            try:
                self._update_runtime_history(loom_ip, running=None, speed_rpm=rpm, source="speed")
            except Exception:
                self.log.exception("Runtime speed update failed for %s", loom_ip)
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "speed", exc)
            raise

    async def query_total_picks(self, loom_ip: str) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, bytes([CMD_TOTAL_PICKS, 0x00]))
            if frame.cmd != CMD_TOTAL_PICKS or frame.length != 4:
                raise core.ProtocolError(f"invalid total-picks reply cmd=0x{frame.cmd:02X} len={frame.length}")
            total = self._u32_from_payload(frame.payload)
            result = {"loom_ip": loom_ip, "total_picks": total, "raw_hex": frame.as_hex()}
            self._remember(loom_ip, "total_picks", result, summary=f"Total picks {total}")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "total_picks", exc)
            raise

    async def query_basic_config(self, loom_ip: str) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, bytes([CMD_BASIC_CONFIG, 0x01, 0x55]))
            if frame.cmd != CMD_BASIC_CONFIG:
                raise core.ProtocolError(f"invalid basic-config reply cmd=0x{frame.cmd:02X}")
            payload = frame.payload
            res, data = self._read_result_payload(payload, expected_res=0x55, min_data_len=22)
            if res not in (None, 0x55):
                raise core.ProtocolError(f"basic-config rejected: {RESULT_TEXT.get(res, hex(res))}")
            if len(data) < 22:
                raise core.ProtocolError(f"basic-config data too short: {len(data)}")
            data = data[:22]
            serial_bytes = data[4:10]
            software_bytes = data[10:22]
            software_text = software_bytes.decode("ascii", errors="replace").rstrip("\x00 ")
            software_kind = "version" if re.search(r"\d", software_text or "") else "id"
            result = {
                "loom_ip": loom_ip,
                "res": res if res is not None else 0x55,
                "loom_type": "Terry" if data[0] == 1 else "Flat",
                "loom_model": "2FAST" if data[1] == 1 else "Evo",
                "jacquard": bool(data[2] == 1),
                "n_beams": data[3],
                "serial_number": serial_bytes.decode("ascii", errors="replace").rstrip("\x00 "),
                "serial_number_raw_hex": serial_bytes.hex().upper(),
                "software_version": software_text,
                "software_version_kind": software_kind,
                "software_version_raw_hex": software_bytes.hex().upper(),
                "raw_hex": frame.as_hex(),
            }
            self._remember(loom_ip, "basic_config", result, summary=f"{result['loom_model']} {result['software_version']}")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "basic_config", exc)
            raise

    async def query_density(self, loom_ip: str) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, bytes([CMD_DENSITY, 0x01, 0x00]))
            if frame.cmd != CMD_DENSITY:
                raise core.ProtocolError(f"invalid density reply cmd=0x{frame.cmd:02X}")
            res, data = self._read_result_payload(frame.payload, expected_res=0x00, min_data_len=35)
            if res not in (None, 0x00):
                raise core.ProtocolError(f"density read rejected: {RESULT_TEXT.get(res, hex(res))}")
            if len(data) < 35:
                raise core.ProtocolError(f"density data too short: {len(data)}")
            dens = [core.u16(data[i], data[i+1]) for i in range(0, 32, 2)]
            unit = data[32]
            int_ext = data[33]
            code = data[34]
            result = {
                "loom_ip": loom_ip,
                "densities": dens,
                "selected_density": dens[0] if dens else None,
                "unit": unit,
                "unit_text": UNIT_DENSITY.get(unit, f"unit {unit}"),
                "int_ext": int_ext,
                "code": code,
                "raw_hex": frame.as_hex(),
            }
            self._remember(loom_ip, "density", result, summary=f"Density {result['selected_density']} {result['unit_text']}")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "density", exc)
            raise

    async def query_pattern_current(self, loom_ip: str) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, bytes([CMD_PATTERN, 0x01, 0x00]))
            if frame.cmd != CMD_PATTERN:
                raise core.ProtocolError(f"invalid pattern-current reply cmd=0x{frame.cmd:02X}")
            res, data = self._read_result_payload(frame.payload, expected_res=0x00, min_data_len=33)
            if res not in (None, 0x00):
                raise core.ProtocolError(f"pattern current rejected: {RESULT_TEXT.get(res, hex(res))}")
            if len(data) < 33:
                raise core.ProtocolError(f"pattern current data too short: {len(data)}")
            data = data[:33]
            result = {
                "loom_ip": loom_ip,
                "length_steps": core.u32(data[0], data[1], data[2], data[3]),
                "name": data[4:28].decode("ascii", errors="replace").rstrip("\x00 "),
                "step": core.u32(data[28], data[29], data[30], data[31]),
                "colour": data[32],
                "raw_hex": frame.as_hex(),
            }
            self._remember(loom_ip, "pattern_current", result, summary=f"Current pattern {result['name']}")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "pattern_current", exc)
            raise

    async def query_pattern_info(self, loom_ip: str, pattern_name: str) -> Dict[str, Any]:
        try:
            payload = bytes([CMD_PATTERN, 0x19, 0x01]) + self._ensure_ascii_24(pattern_name)
            frame = await self._request(loom_ip, payload)
            if frame.cmd != CMD_PATTERN:
                raise core.ProtocolError(f"invalid pattern-info reply cmd=0x{frame.cmd:02X}")
            res, data = self._read_result_payload(frame.payload, expected_res=0x01, min_data_len=8)
            if res not in (None, 0x01):
                raise core.ProtocolError(f"pattern info rejected: {RESULT_TEXT.get(res, hex(res))}")
            if len(data) < 8:
                raise core.ProtocolError(f"pattern info data too short: {len(data)}")
            data = data[:8]
            result = {
                "loom_ip": loom_ip,
                "pattern_name": pattern_name,
                "exists": bool(data[0]),
                "in_execution": bool(data[1]),
                "pattern_format": data[2],
                "pattern_format_text": PATTERN_FORMAT.get(data[2], f"0x{data[2]:02X}"),
                "loom_running": bool(data[3]),
                "length_steps": core.u32(data[4], data[5], data[6], data[7]),
                "raw_hex": frame.as_hex(),
            }
            self._remember(loom_ip, "pattern_info", result, summary=f"Pattern info {pattern_name}")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "pattern_info", exc)
            raise

    async def send_pattern(self, loom_ip: str, pattern_name: str) -> Dict[str, Any]:
        try:
            payload = bytes([CMD_PATTERN, 0x19, 0x02]) + self._ensure_ascii_24(pattern_name)
            frame = await self._request(loom_ip, payload)
            if frame.cmd != CMD_PATTERN:
                raise core.ProtocolError(f"invalid pattern-send reply cmd=0x{frame.cmd:02X}")
            res, data = self._read_result_payload(frame.payload, expected_res=0x02, min_data_len=8)
            if res in (0x77, 0x99, 0xAA):
                raise core.ProtocolError(f"pattern send rejected: {RESULT_TEXT.get(res, hex(res))}")
            if res not in (None, 0x02):
                raise core.ProtocolError(f"pattern send unexpected result: {RESULT_TEXT.get(res, hex(res))}")
            result = {"loom_ip": loom_ip, "pattern_name": pattern_name, "raw_hex": frame.as_hex()}
            if len(data) >= 8:
                data = data[:8]
                result.update({
                    "exists": bool(data[0]),
                    "in_execution": bool(data[1]),
                    "pattern_format": data[2],
                    "pattern_format_text": PATTERN_FORMAT.get(data[2], f"0x{data[2]:02X}"),
                    "loom_running": bool(data[3]),
                    "length_steps": core.u32(data[4], data[5], data[6], data[7]),
                })
            self._remember(loom_ip, "pattern_send", result, summary=f"Pattern send {pattern_name}")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "pattern_send", exc)
            raise

    async def query_lamp_tree(self, loom_ip: str) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, bytes([CMD_LAMP, 0x02, 0x55, 0x00]))

            # The specification documents the request format, but it does not clearly show
            # the reply format for the read-status case. Some looms reply with an empty ACK-
            # like frame instead of a 2-byte payload. Treat that as "no detailed lamp state
            # provided" rather than a hard failure.
            if frame.cmd == CMD_LAMP and frame.length >= 2:
                command = frame.payload[0]
                mask = frame.payload[1]
                result = {
                    "loom_ip": loom_ip,
                    "command": command,
                    "mask": mask,
                    "mask_hex": f"0x{mask:02X}",
                    "call_1": bool(mask & 0x80),
                    "call_2": bool(mask & 0x40),
                    "call_3": bool(mask & 0x20),
                    "raw_hex": frame.as_hex(),
                }
                self._remember(loom_ip, "lamp_tree", result, summary=f"Lamp mask 0x{mask:02X}")
                return result

            if frame.length == 0:
                result = {
                    "loom_ip": loom_ip,
                    "command": frame.cmd,
                    "mask": None,
                    "mask_hex": None,
                    "call_1": None,
                    "call_2": None,
                    "call_3": None,
                    "raw_hex": frame.as_hex(),
                    "notes": ["Loom accepted the lamp-read request but did not return detailed lamp state."],
                }
                self._remember(loom_ip, "lamp_tree", result, summary="Lamp read: empty ACK / no state")
                return result

            raise core.ProtocolError(f"invalid lamp reply cmd=0x{frame.cmd:02X} len={frame.length}")
        except Exception as exc:
            self._wrap_error(loom_ip, "lamp_tree", exc)
            raise

    async def write_lamp_tree(self, loom_ip: str, mask: int) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, bytes([CMD_LAMP, 0x02, 0x01, mask & 0xFF]))
            if frame.cmd not in (CMD_LAMP, 0x00):
                raise core.ProtocolError(f"invalid lamp-write reply cmd=0x{frame.cmd:02X} len={frame.length}")
            result = {
                "loom_ip": loom_ip,
                "mask": mask & 0xFF,
                "mask_hex": f"0x{mask & 0xFF:02X}",
                "reply_cmd": frame.cmd,
                "reply_len": frame.length,
                "reply_raw_hex": frame.as_hex(),
            }
            if frame.length == 0:
                result["notes"] = ["Loom returned an empty ACK-like lamp-write reply."]
            self._remember(loom_ip, "lamp_tree", result, summary=f"Lamp write 0x{mask & 0xFF:02X}")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "lamp_tree", exc)
            raise

    async def query_preselection(self, loom_ip: str) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, bytes([CMD_PRESELECTION, 0x01, 0x55]))
            if frame.cmd != CMD_PRESELECTION:
                raise core.ProtocolError(f"invalid preselection reply cmd=0x{frame.cmd:02X}")
            res, data = self._read_result_payload(frame.payload, expected_res=0x55, min_data_len=14)
            if res not in (None, 0x55):
                raise core.ProtocolError(f"preselection read rejected: {RESULT_TEXT.get(res, hex(res))}")
            if len(data) < 14:
                raise core.ProtocolError(f"preselection data too short: {len(data)}")
            data = data[:14]
            unit = data[5]
            result = {
                "loom_ip": loom_ip,
                "enabled": bool(data[0]),
                "stop_on_reach": bool(data[1]),
                "confirm_before_restart": bool(data[2]),
                "delayed_stop": bool(data[3]),
                "manual_reset": bool(data[4]),
                "unit": unit,
                "unit_text": UNIT_PRESELECTION.get(unit, f"unit {unit}"),
                "preselection_value": core.u32(data[6], data[7], data[8], data[9]),
                "counter_value": core.u32(data[10], data[11], data[12], data[13]),
                "raw_hex": frame.as_hex(),
            }
            self._remember(loom_ip, "preselection", result, summary=f"Preselection {result['preselection_value']} {result['unit_text']}")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "preselection", exc)
            raise

    async def write_preselection(self, loom_ip: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            unit = int(params["unit"])
            pvalue = int(params["preselection_value"])
            counter = int(params.get("counter_value", 0))
            data = bytes([
                1 if params.get("enabled") else 0,
                1 if params.get("stop_on_reach") else 0,
                1 if params.get("confirm_before_restart") else 0,
                1 if params.get("delayed_stop") else 0,
                1 if params.get("manual_reset") else 0,
                unit & 0xFF,
                (pvalue >> 24) & 0xFF, (pvalue >> 16) & 0xFF, (pvalue >> 8) & 0xFF, pvalue & 0xFF,
                (counter >> 24) & 0xFF, (counter >> 16) & 0xFF, (counter >> 8) & 0xFF, counter & 0xFF,
            ])
            request = bytes([CMD_PRESELECTION, 0x0F, 0x01]) + data
            frame = await self._request(loom_ip, request)
            if frame.cmd != CMD_PRESELECTION:
                raise core.ProtocolError(f"invalid preselection-write reply cmd=0x{frame.cmd:02X}")
            res = frame.payload[0] if frame.payload else None
            if res not in (0x01,):
                raise core.ProtocolError(f"preselection write rejected: {RESULT_TEXT.get(res, hex(res) if res is not None else 'no reply')}" )
            result = {"loom_ip": loom_ip, "written": dict(params), "reply_raw_hex": frame.as_hex()}
            self._remember(loom_ip, "preselection", result, summary="Preselection write OK")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "preselection", exc)
            raise

    async def reset_preselection(self, loom_ip: str) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, bytes([CMD_PRESELECTION, 0x01, 0x02]))
            if frame.cmd != CMD_PRESELECTION:
                raise core.ProtocolError(f"invalid preselection-reset reply cmd=0x{frame.cmd:02X}")
            res = frame.payload[0] if frame.payload else None
            if res not in (0x02,):
                raise core.ProtocolError(f"preselection reset rejected: {RESULT_TEXT.get(res, hex(res) if res is not None else 'no reply')}" )
            result = {"loom_ip": loom_ip, "reply_raw_hex": frame.as_hex(), "reset": True}
            self._remember(loom_ip, "preselection", result, summary="Preselection reset OK")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "preselection", exc)
            raise

    def _ftp_settings_for(self, loom_ip: str) -> Dict[str, Any]:
        cfg = self.registry.config_by_ip.get(loom_ip, {}) or {}
        return {
            "host": loom_ip,
            "port": int(cfg.get("ftp_port", self.config.get("ftp_port", DEFAULT_FTP_PORT))),
            "username": str(cfg.get("ftp_username", self.config.get("ftp_username", DEFAULT_FTP_USER))),
            "password": str(cfg.get("ftp_password", self.config.get("ftp_password", DEFAULT_FTP_PASSWORD))),
            "app_dir": str(cfg.get("ftp_application_dir", self.config.get("ftp_application_dir", DEFAULT_FTP_APP_DIR))),
        }

    @staticmethod
    def _parse_csv_rows(text: str, limit: int = 12) -> Dict[str, Any]:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return {"rows": [], "header": []}
        try:
            rows = list(csv.reader(lines))
        except Exception:
            rows = [[ln] for ln in lines]
        header = rows[0] if rows and any(ch.isalpha() for ch in ''.join(rows[0])) else []
        data_rows = rows[1:] if header else rows
        trimmed = [[str(col).strip() for col in row] for row in data_rows[-limit:]]
        return {"header": header, "rows": trimmed}

    async def query_error_logs(self, loom_ip: str) -> Dict[str, Any]:
        settings = self._ftp_settings_for(loom_ip)

        def _fetch() -> Dict[str, Any]:
            out: Dict[str, Any] = {"loom_ip": loom_ip, "source": "ftp", "notes": []}
            with ftplib.FTP() as ftp:
                ftp.connect(settings["host"], settings["port"], timeout=6)
                ftp.login(settings["username"], settings["password"])
                app_dir = settings["app_dir"].rstrip('/')
                for filename, key in (("ERRORSerr.csv", "errors"), ("EVENTSeve.csv", "events")):
                    target = f"{app_dir}/{filename}"
                    bio = io.BytesIO()
                    ftp.retrbinary(f"RETR {target}", bio.write)
                    parsed = self._parse_csv_rows(bio.getvalue().decode('utf-8', errors='replace'), limit=12)
                    parsed["path"] = target
                    out[key] = parsed
            if not out.get("errors", {}).get("rows"):
                out["notes"].append("No error rows returned from FTP log file")
            return out

        try:
            result = await asyncio.to_thread(_fetch)
            self._remember(loom_ip, "error_logs", result, summary="Error/event logs loaded")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "error_logs", exc)
            raise

    async def remote_stop(self, loom_ip: str) -> Dict[str, Any]:
        try:
            frame = await self._request(loom_ip, bytes([CMD_REMOTE_CONTROL, 0x01, 0x01]))
            accepted = False
            notes: List[str] = []
            if frame.cmd == CMD_REMOTE_CONTROL and frame.length >= 1 and frame.payload[0] in (0x01, 0x00):
                accepted = True
            elif frame.cmd == CMD_REMOTE_CONTROL and frame.length == 0:
                accepted = True
                notes.append("Empty remote-stop ACK accepted")
            elif frame.cmd == 0x00 and frame.length == 0:
                accepted = True
                notes.append("Generic empty ACK accepted")
            if not accepted:
                raise core.ProtocolError(f"remote stop rejected: {frame.as_hex()}")
            result = {"loom_ip": loom_ip, "reply_raw_hex": frame.as_hex(), "cmd": 1, "action": "stop"}
            if notes:
                result["notes"] = notes
            self._remember(loom_ip, "remote_stop", result, summary="Remote stop accepted")
            return result
        except Exception as exc:
            self._wrap_error(loom_ip, "remote_stop", exc)
            raise


# ---------------- helpers for CLI ----------------
def load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return core.default_config()
    with Path(path).open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    base = core.default_config()
    base.update(cfg)
    base.setdefault("write_actions_enabled", True)
    base.setdefault("config_backup_dir", "config_backups")
    for item in base.get("looms", []):
        item.setdefault("enabled", True)
        item.setdefault("poll_status_every_seconds", 5)
        item.setdefault("poll_full_status_every_seconds", 10)
    base["__config_path"] = str(Path(path).resolve())
    return base


def setup_logging(level_name: str, log_dir: str = "logs") -> Dict[str, str]:
    level = getattr(logging, level_name.upper(), logging.INFO)
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    app_log = str((Path(log_dir) / f"loom_host_master_{stamp}.log").resolve())
    err_log = str((Path(log_dir) / f"loom_host_master_error_{stamp}.log").resolve())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_all = logging.FileHandler(app_log, encoding="utf-8")
    file_all.setLevel(level)
    file_all.setFormatter(formatter)
    root.addHandler(file_all)

    file_err = logging.FileHandler(err_log, encoding="utf-8")
    file_err.setLevel(logging.WARNING)
    file_err.setFormatter(formatter)
    root.addHandler(file_err)

    return {"log_dir": str(Path(log_dir).resolve()), "app_log": app_log, "error_log": err_log}


async def tcp_connectivity_test(ip: str, port: int, timeout_s: float = 2.0) -> Dict[str, Any]:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout_s)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {"ok": True, "ip": ip, "port": port}
    except Exception as exc:
        return {"ok": False, "ip": ip, "port": port, "error": f"{type(exc).__name__}: {exc}"}


async def bind_test(host: str, port: int, udp: bool) -> Dict[str, Any]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        if not udp:
            sock.listen(1)
        return {"ok": True, "host": host, "port": port, "transport": "udp" if udp else "tcp"}
    except Exception as exc:
        return {"ok": False, "host": host, "port": port, "transport": "udp" if udp else "tcp", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        sock.close()


async def run_service(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if getattr(args, "_log_info", None):
        config["__log_info"] = dict(args._log_info)
    app = MasterApp(config)
    app.started_at = time.time()
    app.startup_status = {"tcp": None, "udp": None, "http": None}
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:
            pass

    background_tasks: List[asyncio.Task[Any]] = []
    if args.snapshot_interval > 0:
        background_tasks.append(asyncio.create_task(app.print_snapshot_loop(args.snapshot_interval)))

    try:
        await app.run_until_stopped()
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await app.stop()
    return 0


async def run_selftest(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if getattr(args, "_log_info", None):
        config["__log_info"] = dict(args._log_info)
    host = config.get("listen_host", "0.0.0.0")
    host_port = int(config.get("host_port", core.DEFAULT_HOST_PORT))
    report: Dict[str, Any] = {
        "listener_tcp_bind": await bind_test(host, host_port, udp=False),
        "listener_udp_bind": await bind_test(host, host_port, udp=True),
        "looms": [],
    }

    app = MasterApp(config)
    for loom in config.get("looms", []):
        ip = loom["ip"]
        ts_port = int(loom.get("ts_port", core.DEFAULT_TS_PORT))
        entry: Dict[str, Any] = {
            "name": loom.get("name", ip),
            "ip": ip,
            "ts_connectivity": await tcp_connectivity_test(ip, ts_port, timeout_s=float(config.get("ts_connect_timeout_seconds", 3.0))),
        }
        try:
            entry["status_query"] = await app.query_status(ip)
        except Exception as exc:
            entry["status_query"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        report["looms"].append(entry)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


async def run_query(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    app = MasterApp(config)
    method = args.action
    try:
        if method == "status":
            result = await app.query_status(args.loom_ip)
        elif method == "full-status":
            result = await app.query_full_status(args.loom_ip)
        elif method == "speed":
            result = await app.query_speed(args.loom_ip)
        elif method == "total-picks":
            result = await app.query_total_picks(args.loom_ip)
        elif method == "basic-config":
            result = await app.query_basic_config(args.loom_ip)
        elif method == "density":
            result = await app.query_density(args.loom_ip)
        elif method == "pattern-current":
            result = await app.query_pattern_current(args.loom_ip)
        elif method == "pattern-info":
            result = await app.query_pattern_info(args.loom_ip, args.pattern_name)
        elif method == "pattern-send":
            result = await app.send_pattern(args.loom_ip, args.pattern_name)
        elif method == "lamp-read":
            result = await app.query_lamp_tree(args.loom_ip)
        elif method == "lamp-write":
            result = await app.write_lamp_tree(args.loom_ip, args.mask)
        elif method == "preselection-read":
            result = await app.query_preselection(args.loom_ip)
        elif method == "preselection-write":
            result = await app.write_preselection(args.loom_ip, {
                "enabled": args.enabled,
                "stop_on_reach": args.stop_on_reach,
                "confirm_before_restart": args.confirm_before_restart,
                "delayed_stop": args.delayed_stop,
                "manual_reset": args.manual_reset,
                "unit": args.unit,
                "preselection_value": args.preselection_value,
                "counter_value": args.counter_value,
            })
        elif method == "preselection-reset":
            result = await app.reset_preselection(args.loom_ip)
        elif method == "popup":
            await app.send_popup(args.loom_ip, args.message)
            result = {"ok": True, "loom_ip": args.loom_ip, "message": args.message}
        elif method == "remote-stop":
            result = await app.remote_stop(args.loom_ip)
        elif method == "raw":
            result = await app.send_raw_hex(args.loom_ip, args.hex)
        else:
            raise ValueError(f"unsupported action: {method}")
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    finally:
        await app.stop()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SMIT loom host/master with interactive dashboard")
    p.add_argument("--config", default=None, help="Path to JSON config file")
    p.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")

    sub = p.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run host service, polling, and dashboard")
    run_p.add_argument("--snapshot-interval", type=float, default=0.0, help="Optional periodic JSON snapshot log interval in seconds")

    sub.add_parser("selftest", help="Check bindability and perform a one-shot status read per configured loom")

    def query_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        q = sub.add_parser(name, help=help_text)
        q.add_argument("--loom-ip", required=True, help="Loom IP address")
        return q

    query_parser("status", "Read basic loom status")
    query_parser("full-status", "Read QT5 full status / Industry4.0 telegram")
    query_parser("speed", "Read dedicated speed command")
    query_parser("total-picks", "Read dedicated total-pick counter")
    query_parser("basic-config", "Read loom basic configuration and software version")
    query_parser("density", "Read pick density table")
    query_parser("pattern-current", "Read current pattern summary")
    qp = query_parser("pattern-info", "Read selected pattern info")
    qp.add_argument("--pattern-name", required=True, help="Pattern file name, <=24 ASCII chars")
    qps = query_parser("pattern-send", "Send selected pattern into execution")
    qps.add_argument("--pattern-name", required=True, help="Pattern file name, <=24 ASCII chars")
    query_parser("lamp-read", "Read lamp tree state")
    qlw = query_parser("lamp-write", "Write lamp tree state")
    qlw.add_argument("--mask", required=True, type=lambda x: int(x, 0), help="Lamp mask, e.g. 0x80 for call 1")
    query_parser("preselection-read", "Read preselection settings")
    qpw = query_parser("preselection-write", "Write preselection settings")
    qpw.add_argument("--enabled", action="store_true")
    qpw.add_argument("--stop-on-reach", action="store_true")
    qpw.add_argument("--confirm-before-restart", action="store_true")
    qpw.add_argument("--delayed-stop", action="store_true")
    qpw.add_argument("--manual-reset", action="store_true")
    qpw.add_argument("--unit", required=True, type=int, choices=sorted(UNIT_PRESELECTION.keys()))
    qpw.add_argument("--preselection-value", required=True, type=int)
    qpw.add_argument("--counter-value", type=int, default=0)
    query_parser("preselection-reset", "Reset preselection counter")
    qpopup = query_parser("popup", "Send popup to loom")
    qpopup.add_argument("--message", required=True, help="Popup message text")
    query_parser("remote-stop", "Send remote stop command")
    qraw = query_parser("raw", "Send raw hex frame")
    qraw.add_argument("--hex", required=True, help="Hex string to transmit, e.g. '0A 00'")

    return p


async def async_main(args: argparse.Namespace) -> int:
    if args.command == "run":
        return await run_service(args)
    if args.command == "selftest":
        return await run_selftest(args)
    args.action = args.command
    return await run_query(args)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    args._log_info = setup_logging(args.log_level, cfg.get("log_dir", "logs"))
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
