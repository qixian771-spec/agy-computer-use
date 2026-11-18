"""Native MacBook Dynamic Island & Silky AI Cursor Bridge (v3.1).

Replaces the old frosted-glass bottom pill with a hardware-fused MacBook Notch
Dynamic Island (`agy-hud`):
- Completely tucked inside the MacBook notch when idle (0% CPU).
- Streams live AI activity (`open` / `click` / `type` / `scroll` / `batch` / `done`)
  with 60fps spring physics & dancing gradient waveform bars over localhost UDP.
- Handles Login / QR Scan / OAuth / CAPTCHA / macOS Privacy Permission handoffs
  right inside the Dynamic Island with one-click window pop-out & auto-resume.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class NativeHUDBridge:
    def __init__(self) -> None:
        self.port = int(os.environ.get("AGY_HUD_PORT", "47866"))
        self.enabled = sys.platform == "darwin" and os.environ.get("AGY_DISABLE_HUD") != "1"
        uid = os.getuid() if hasattr(os, "getuid") else 0
        self.state_file = Path(f"/tmp/agy_hud_state_{uid}.json")
        self.pid_file = Path(f"/tmp/agy_hud_{uid}.pid")
        self._sock: Optional[socket.socket] = None
        self._last_spawn_attempt: float = 0.0
        self._reset_ts: float = time.time()
        self._cached_mtime: float = 0.0
        self._cached_state: Dict[str, Any] = {}

    def _find_hud_binary(self) -> Optional[Path]:
        if not self.enabled:
            return None
        root = Path(__file__).resolve().parent.parent
        candidates = [
            root / "bin" / "agy-hud",
            root / "bin" / "darwin" / "agy-hud",
            Path.home() / ".gemini" / "agy-computer-use" / "bin" / "agy-hud",
        ]
        for c in candidates:
            if c.is_file() and os.access(c, os.X_OK):
                return c
        return None

    def _is_daemon_alive(self) -> bool:
        try:
            if not self.pid_file.is_file():
                return False
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
            if pid <= 0:
                return False
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def ensure_running(self) -> bool:
        if not self.enabled:
            return False
        if self._is_daemon_alive():
            return True
        now = time.time()
        if now - self._last_spawn_attempt < 2.0:
            return False
        self._last_spawn_attempt = now
        hud_bin = self._find_hud_binary()
        if hud_bin is None:
            return False
        try:
            subprocess.Popen(
                [str(hud_bin)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(8):
                time.sleep(0.025)
                if self._is_daemon_alive():
                    return True
            return True
        except Exception:
            return False

    def _send(self, payload: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            if not self.ensure_running():
                return
            if self._sock is None:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sock.setblocking(False)
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._sock.sendto(raw, ("127.0.0.1", self.port))
        except Exception:
            pass

    def read_state(self, force: bool = False) -> Dict[str, Any]:
        if not self.enabled:
            return {}
        try:
            st = self.state_file.stat()
            if force or st.st_mtime != self._cached_mtime:
                self._cached_mtime = st.st_mtime
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._cached_state = data
            return self._cached_state
        except Exception:
            return self._cached_state

    def check_stop_clicked(self) -> bool:
        st = self.read_state()
        stop_ts = float(st.get("stop_ts") or 0.0)
        if stop_ts > self._reset_ts:
            self._reset_ts = stop_ts + 0.001
            return True
        return False

    def clear_stop(self) -> None:
        self._reset_ts = time.time()

    def check_auth_resolved(self, since_ts: float) -> bool:
        st = self.read_state(force=True)
        resolved_ts = float(st.get("auth_resolved_ts") or 0.0)
        return resolved_ts > since_ts

    def is_auth_active(self) -> bool:
        if not self.enabled:
            return False
        st = self.read_state()
        return bool(st.get("auth_active", False))

    def is_watching(self, fallback: bool = False) -> bool:
        if not self.enabled:
            return fallback
        st = self.read_state()
        if "watching" in st:
            return bool(st["watching"])
        return fallback

    @staticmethod
    def _attach_target_meta(
        msg: Dict[str, Any],
        *,
        pid: int = 0,
        window_id: int = 0,
        app: str = "",
        bounds: Optional[List[float]] = None,
        inset_top: float = 0.0,
        content_size: Optional[List[float]] = None,
    ) -> None:
        if pid > 0:
            msg["pid"] = int(pid)
        if window_id > 0:
            msg["window_id"] = int(window_id)
        if app:
            msg["app"] = app
        if bounds and len(bounds) == 4:
            msg["bounds"] = [float(v) for v in bounds]
        if inset_top >= 0:
            msg["inset_top"] = float(inset_top)
        if content_size and len(content_size) == 2:
            msg["content_size"] = [float(v) for v in content_size]

    def notify_status(
        self,
        text: str,
        *,
        phase: str = "work",
        pid: int = 0,
        window_id: int = 0,
        app: str = "",
        bounds: Optional[List[float]] = None,
        inset_top: float = 0.0,
        content_size: Optional[List[float]] = None,
        done: bool = False,
        auto_hide_sec: float = 5.5,
    ) -> None:
        msg: Dict[str, Any] = {
            "cmd": "status",
            "text": text,
            "phase": "done" if done else phase,
            "done": done,
            "auto_hide_sec": auto_hide_sec,
        }
        self._attach_target_meta(
            msg,
            pid=pid,
            window_id=window_id,
            app=app,
            bounds=bounds,
            inset_top=inset_top,
            content_size=content_size,
        )
        self._send(msg)

    def notify_cursor(
        self,
        nx: float,
        ny: float,
        *,
        kind: str = "move",
        badge: str = "AI",
        text: str = "",
        phase: str = "click",
        pid: int = 0,
        window_id: int = 0,
        app: str = "",
        bounds: Optional[List[float]] = None,
        inset_top: float = 0.0,
        content_size: Optional[List[float]] = None,
    ) -> None:
        msg: Dict[str, Any] = {
            "cmd": "cursor",
            "nx": float(nx),
            "ny": float(ny),
            "kind": kind,
            "badge": badge,
            "phase": phase,
            "auto_hide_sec": 5.5,
        }
        if text:
            msg["text"] = text
        self._attach_target_meta(
            msg,
            pid=pid,
            window_id=window_id,
            app=app,
            bounds=bounds,
            inset_top=inset_top,
            content_size=content_size,
        )
        self._send(msg)

    def notify_watch(
        self,
        watching: bool,
        *,
        text: str = "",
        pid: int = 0,
        window_id: int = 0,
        app: str = "",
        bounds: Optional[List[float]] = None,
        inset_top: float = 0.0,
        content_size: Optional[List[float]] = None,
    ) -> None:
        msg: Dict[str, Any] = {
            "cmd": "watch",
            "watching": bool(watching),
        }
        if text:
            msg["text"] = text
        self._attach_target_meta(
            msg,
            pid=pid,
            window_id=window_id,
            app=app,
            bounds=bounds,
            inset_top=inset_top,
            content_size=content_size,
        )
        self._send(msg)

    def notify_auth(
        self,
        title: str,
        text: str,
        *,
        pop_window: bool = True,
        pid: int = 0,
        window_id: int = 0,
        app: str = "",
        bounds: Optional[List[float]] = None,
        inset_top: float = 0.0,
        content_size: Optional[List[float]] = None,
    ) -> None:
        msg: Dict[str, Any] = {
            "cmd": "auth",
            "title": title,
            "text": text,
            "pop_window": bool(pop_window),
            "phase": "auth",
        }
        self._attach_target_meta(
            msg,
            pid=pid,
            window_id=window_id,
            app=app,
            bounds=bounds,
            inset_top=inset_top,
            content_size=content_size,
        )
        self._send(msg)

    def notify_auth_done(
        self,
        text: str = "✓ 登录授权已完成 · 已切回后台继续执行",
        *,
        pid: int = 0,
        window_id: int = 0,
        app: str = "",
    ) -> None:
        msg: Dict[str, Any] = {
            "cmd": "auth_done",
            "text": text,
            "phase": "work",
        }
        self._attach_target_meta(msg, pid=pid, window_id=window_id, app=app)
        self._send(msg)

    def notify_permission(
        self,
        title: str = "需要开启 macOS 屏幕录制 / 辅助功能权限",
        text: str = "点击下方按钮打开系统设置勾选 Antigravity，完成后点「已开启」",
        pref_url: str = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    ) -> None:
        self._send(
            {
                "cmd": "permission",
                "title": title,
                "text": text,
                "pref_url": pref_url,
                "phase": "auth",
            }
        )


HUD = NativeHUDBridge()
